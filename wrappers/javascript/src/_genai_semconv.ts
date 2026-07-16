/**
 * Single source of truth for the OpenTelemetry GenAI attributes both
 * telemetry sinks stamp on a span. {@link OtelSpanSink} (caller's tracer)
 * and {@link OtlpSink} (direct OTLP/HTTP) emit the *same* attribute names
 * from the *same* event keys — this module exists so the two can never
 * drift. A dashboard query for ``gen_ai.usage.input_tokens`` resolves to
 * one mapping regardless of which sink produced the span.
 *
 * Posture: we emit the latest (semconv 1.41.x) GenAI attribute names
 * unconditionally; the deprecated ``gen_ai.system`` is deliberately not
 * emitted. ``OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental``
 * is a no-op for our manually-stamped attributes because we are already
 * on the latest — there is no downgrade-to-``gen_ai.system`` toggle, and
 * defaulting a greenfield SDK to a deprecated name would be wrong. Keep
 * the schema URL ({@link OTEL_SCHEMA_URL}) in lockstep with this list.
 *
 * Two event-key shapes feed these attributes, and both must be covered:
 *
 *   1. Dotted, OTel-spec keys (``event["gen_ai.provider.name"]`` …).
 *      Written by the transport's URL derivation (``_genai.ts``) and the
 *      opt-in body extractor (``_genai_body.ts``).
 *
 *   2. Flat, wire-schema keys (``event.gen_ai_system`` …). Written by the
 *      framework adapters (Vercel AI SDK, LangChain, OpenAI Agents) so
 *      events already match ``TelemetryEventInput`` (the ingestion
 *      schema) without a rename pass. Any such event routed through a
 *      sink would otherwise lose its GenAI attributes entirely.
 *
 * Each descriptor lists the dotted key first, then the flat alias. The
 * deprecated ``gen_ai_system`` flat key maps onto the modern
 * ``gen_ai.provider.name`` attribute — switch-over, never dual-emit.
 */

import type { TelemetryEvent } from "./sinks.js";

/** A GenAI attribute and the event keys it can be sourced from. */
interface GenAiAttrSpec {
  /** Emitted attribute name (latest semconv; never ``gen_ai.system``). */
  readonly attr: string;
  /**
   * Event keys to read, in priority order. The dotted OTel-spec key
   * (written by the transport / body extractor) is preferred; the flat
   * wire-schema alias (written by framework adapters) is the fallback.
   */
  readonly keys: readonly string[];
}

/** String-valued GenAI attributes, dotted-key-first with flat aliases. */
export const GENAI_STRING_ATTRS: readonly GenAiAttrSpec[] = [
  // ``gen_ai.system`` is DEPRECATED in the OTel registry; the modern
  // attribute is ``gen_ai.provider.name``. The flat ``gen_ai_system``
  // alias (framework adapters / wire schema) maps onto the modern name.
  { attr: "gen_ai.provider.name", keys: ["gen_ai.provider.name", "gen_ai_system"] },
  { attr: "gen_ai.operation.name", keys: ["gen_ai.operation.name"] },
  { attr: "gen_ai.request.model", keys: ["gen_ai.request.model", "gen_ai_model"] },
  { attr: "gen_ai.response.model", keys: ["gen_ai.response.model"] },
];

/** Integer-valued GenAI attributes, dotted-key-first with flat aliases. */
export const GENAI_NUMERIC_ATTRS: readonly GenAiAttrSpec[] = [
  {
    attr: "gen_ai.usage.input_tokens",
    keys: ["gen_ai.usage.input_tokens", "gen_ai_input_tokens"],
  },
  {
    attr: "gen_ai.usage.output_tokens",
    keys: ["gen_ai.usage.output_tokens", "gen_ai_output_tokens"],
  },
];

/** Boolean-valued GenAI attributes (body-extractor only; no flat alias). */
export const GENAI_BOOL_ATTRS: readonly GenAiAttrSpec[] = [
  { attr: "gen_ai.request.stream", keys: ["gen_ai.request.stream"] },
];

/**
 * Stamp the GenAI semconv attributes on a span via type-specific
 * setters. Sink-agnostic: {@link OtelSpanSink} passes ``span.setAttribute``
 * shims and {@link OtlpSink} passes shims that push OTLP attribute
 * objects. Reads the first present source key per attribute (dotted
 * before flat) so the two event shapes converge on identical output.
 */
export function stampGenAiAttributes(
  event: TelemetryEvent,
  setters: {
    setString: (key: string, value: string) => void;
    setNumber: (key: string, value: number) => void;
    setBoolean: (key: string, value: boolean) => void;
  },
): void {
  for (const { attr, keys } of GENAI_STRING_ATTRS) {
    const value = firstString(event, keys);
    if (value !== undefined) setters.setString(attr, value);
  }
  for (const { attr, keys } of GENAI_NUMERIC_ATTRS) {
    const value = firstNumber(event, keys);
    if (value !== undefined) setters.setNumber(attr, value);
  }
  for (const { attr, keys } of GENAI_BOOL_ATTRS) {
    const value = firstBoolean(event, keys);
    if (value !== undefined) setters.setBoolean(attr, value);
  }
}

function firstString(event: TelemetryEvent, keys: readonly string[]): string | undefined {
  for (const key of keys) {
    const v = event[key];
    if (typeof v === "string") return v;
  }
  return undefined;
}

function firstNumber(event: TelemetryEvent, keys: readonly string[]): number | undefined {
  for (const key of keys) {
    const v = event[key];
    if (typeof v === "number" && Number.isFinite(v)) return v;
  }
  return undefined;
}

function firstBoolean(event: TelemetryEvent, keys: readonly string[]): boolean | undefined {
  for (const key of keys) {
    const v = event[key];
    if (typeof v === "boolean") return v;
  }
  return undefined;
}
