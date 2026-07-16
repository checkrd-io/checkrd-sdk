/**
 * Stream token capture for OpenAI / Anthropic SSE responses.
 *
 * Two vendor wire formats are parsed here:
 *
 *   - **OpenAI** (`text/event-stream`): each event is a single `data: {...}`
 *     line with a sentinel `data: [DONE]`. Token usage appears in the
 *     last real chunk when the caller set `stream_options.include_usage`.
 *
 *   - **Anthropic** (`text/event-stream`): events carry an `event:` tag
 *     (`message_start`, `message_delta`, `message_stop`, etc). Input
 *     tokens land in `message_start.message.usage`; output tokens are
 *     cumulative in the last `message_delta.usage`.
 *
 * The tee pattern — `Response.body.tee()` — duplicates the stream so the
 * caller keeps one copy for normal iteration while we read the other for
 * token accounting. Without this, the consumer would see an empty body.
 */

import type { TelemetryEvent } from "./batcher.js";
import type { TelemetrySink } from "./sinks.js";
import type { Logger } from "./_logger.js";

/** Vendor label for a captured stream. */
export type StreamVendor = "openai" | "anthropic" | "unknown";

// OTel GenAI usage attribute keys. These MUST match `_genai_body.ts`
// (the body extractor) and the Python streaming tap byte-for-byte —
// they are the cross-runtime wire contract, pinned by the language-
// neutral fixtures in `schemas/genai-fixtures/streaming/`.
const ATTR_INPUT_TOKENS = "gen_ai.usage.input_tokens";
const ATTR_OUTPUT_TOKENS = "gen_ai.usage.output_tokens";
const ATTR_CACHE_READ = "gen_ai.usage.cache_read.input_tokens";
const ATTR_CACHE_CREATION = "gen_ai.usage.cache_creation.input_tokens";
const ATTR_REASONING = "gen_ai.usage.reasoning.output_tokens";

/**
 * Pricing-settlement status carried on a `stream_completion` event.
 *
 * `"untallied"` means the stream ended without a terminal usage frame
 * (client disconnect / truncation). The engine never estimates usage
 * for an abandoned stream (TDD §4.2) — the pre-flight reserve is
 * released on settle-timeout instead. Tallied streams omit this field.
 */
export type PricingStatus = "untallied";

/**
 * Outcome of feeding an ordered set of SSE frames to the usage tap.
 *
 * `usageAttrs` is keyed by the OTel `gen_ai.usage.*` dotted names (same
 * keys the body extractor emits). It is **empty** when the stream was
 * abandoned before its terminal usage frame; in that case
 * `pricingStatus` is `"untallied"`. A fully-tallied stream omits
 * `pricingStatus`.
 */
export interface StreamCaptureResult {
  usageAttrs: Record<string, number>;
  pricingStatus?: PricingStatus;
}

/**
 * Mutable accumulator for the streaming usage tap.
 *
 * The tap is a strict pass-through: it inspects only terminal/metadata
 * frames and never buffers content frames. This state records the raw
 * provider counts as they arrive plus whether the *terminal* usage
 * frame was seen — OpenAI's `include_usage` chunk, or Anthropic's final
 * `message_delta` carrying `output_tokens`. Absence of that frame is
 * what distinguishes an abandoned stream (→ `untallied`) from a
 * complete one, so we track it explicitly rather than inferring from
 * whatever partial numbers happen to be present.
 */
interface StreamUsageState {
  /** OpenAI `prompt_tokens` / `input_tokens`, or Anthropic raw `input_tokens` (cache-exclusive). */
  rawInput: number | null;
  /** OpenAI `completion_tokens` / Anthropic cumulative `output_tokens`. */
  output: number | null;
  /** `prompt_tokens_details.cached_tokens` (OpenAI) / `cache_read_input_tokens` (Anthropic). */
  cacheRead: number | null;
  /** Anthropic `cache_creation_input_tokens` (OpenAI has no equivalent). */
  cacheCreation: number | null;
  /** `completion_tokens_details.reasoning_tokens` (OpenAI). */
  reasoning: number | null;
  /** Whether the terminal usage frame arrived. Gates tallied-vs-untallied. */
  terminalSeen: boolean;
  /** Most recent finish/stop reason, for the emitted event. */
  finishReason: string | null;
  /**
   * Whether `rawInput` EXCLUDES cache and so must be normalized to the
   * inclusive total at finalize. Anthropic's `message_start` input is
   * cache-exclusive (→ true); OpenAI's `prompt_tokens` already includes
   * cached tokens (→ false). Set when the input count is recorded.
   */
  inputExcludesCache: boolean;
}

function newUsageState(): StreamUsageState {
  return {
    rawInput: null,
    output: null,
    cacheRead: null,
    cacheCreation: null,
    reasoning: null,
    terminalSeen: false,
    finishReason: null,
    inputExcludesCache: false,
  };
}

/** Options for {@link captureStreamTokens}. */
export interface CaptureOptions {
  /** Vendor of the upstream response. */
  vendor: StreamVendor;
  /** Identifier baked into the emitted telemetry. */
  requestId: string;
  /** Target URL (for event labeling). */
  url: string;
  /** HTTP method (for event labeling). */
  method: string;
  /** Agent ID for telemetry correlation. */
  agentId: string;
  /** Sink that receives the token-usage event. Required, else caller wouldn't call us. */
  sink: TelemetrySink;
  /** Logger for diagnostics. */
  logger?: Logger;
  /** Start time in Unix ms, used to compute latency when the stream ends. */
  startMs: number;
}

/**
 * Tee a streaming response body so we can count tokens without breaking
 * the consumer's `for await` loop on `response.body`.
 *
 * Returns a fresh Response whose body is one half of the tee; the other
 * half is consumed in the background to extract usage.
 */
export function teeResponseForTokens(
  response: Response,
  opts: CaptureOptions,
): Response {
  if (response.body === null) return response;
  const contentType = response.headers.get("content-type") ?? "";
  if (!contentType.includes("text/event-stream")) return response;
  // Process-wide budget gate. If we can't reserve room for the per-stream
  // worst case, the response passes through un-teed — the consumer still
  // gets the data, the SDK just skips token accounting for this stream.
  // This converts an unbounded N × per-stream memory exposure into a
  // capped, observable counter.
  if (!streamCaptureBudget.acquire(MAX_STREAM_EVENT_BYTES)) {
    opts.logger?.warn(
      "stream token capture skipped: process-wide budget exhausted",
      streamCaptureBudget.diagnostics(),
    );
    return response;
  }
  const [forConsumer, forTelemetry] = response.body.tee();
  // Fire-and-forget token accounting. Failures must never affect the
  // consumer — we only log. The `finally` releases the budget regardless
  // of success / parse error / abort, so a flaky upstream cannot starve
  // future streams.
  void captureStreamTokens(forTelemetry, opts)
    .catch((err: unknown) => {
      opts.logger?.debug("stream token capture failed", { err });
    })
    .finally(() => {
      streamCaptureBudget.release(MAX_STREAM_EVENT_BYTES);
    });
  return new Response(forConsumer, {
    status: response.status,
    statusText: response.statusText,
    headers: response.headers,
  });
}

/**
 * Hard upper bound on the buffered SSE payload between event boundaries.
 * A compromised or hostile upstream (e.g., a MITM in front of an LLM
 * vendor) could otherwise stream bytes without newline terminators
 * indefinitely and exhaust process memory via the tee. The cap converts
 * a remote-controllable OOM into a logged warning that stops the capture
 * without affecting the consumer-side stream.
 */
const MAX_STREAM_EVENT_BYTES = 4 * 1024 * 1024;

/**
 * Process-wide ceiling on memory the token-capture path may hold across
 * all concurrent streams. The per-stream cap above is necessary but not
 * sufficient: under load (1000+ concurrent agent calls) N streams ×
 * `MAX_STREAM_EVENT_BYTES` could still exhaust the heap. The aggregate
 * budget is what makes the capture path bounded.
 *
 * Default 32 MiB = 8 concurrent streams at the per-stream max. This is
 * Sentry's `maxAttachmentSize` × concurrency-ceiling pattern; OTel uses
 * the same shape for span-batch memory.
 *
 * Operators with high concurrency raise via {@link setStreamCaptureBudget};
 * lowering it shifts the trade-off toward dropping captures rather than
 * holding memory.
 */
const DEFAULT_STREAM_CAPTURE_BUDGET_BYTES = 32 * 1024 * 1024;

/**
 * Single-process accountant for stream-capture memory. Exposed via the
 * module-level singleton {@link streamCaptureBudget} so every call site
 * (even ones in different files) sees the same in-use total. Counters
 * are monotonic — `dropped_budget` tracks every refused acquire — so
 * dashboards can alert on "captures we silently skipped" the same way
 * the batcher tracks `droppedBackpressure`.
 *
 * No locking: JavaScript is single-threaded within a runtime context and
 * `acquire`/`release` are synchronous arithmetic. The async stream loop
 * never preempts in the middle of an `inUse += bytes` step. Web Workers
 * / Node worker_threads each get their own module instance and budget,
 * which is the desired isolation.
 */
export class StreamCaptureBudget {
  private inUse = 0;
  private droppedBudget = 0;
  private capacity: number;

  constructor(capacity: number) {
    if (!Number.isFinite(capacity) || capacity < 0) {
      throw new Error(
        `StreamCaptureBudget capacity must be a non-negative finite number; got ${String(capacity)}`,
      );
    }
    this.capacity = capacity;
  }

  /**
   * Reserve `bytes` from the budget. Returns `true` on success — the
   * caller MUST eventually call {@link release} with the same value, or
   * the budget will gradually starve. Returns `false` when the request
   * would exceed capacity; the caller skips capture and continues.
   */
  acquire(bytes: number): boolean {
    if (bytes <= 0) return true; // zero-size reservations are always free
    if (this.inUse + bytes > this.capacity) {
      this.droppedBudget += 1;
      return false;
    }
    this.inUse += bytes;
    return true;
  }

  /** Return `bytes` to the budget. Idempotent at zero. */
  release(bytes: number): void {
    if (bytes <= 0) return;
    this.inUse = Math.max(0, this.inUse - bytes);
  }

  /** Replace the capacity at runtime (operator tuning). */
  setCapacity(bytes: number): void {
    if (!Number.isFinite(bytes) || bytes < 0) {
      throw new Error(
        `StreamCaptureBudget capacity must be a non-negative finite number; got ${String(bytes)}`,
      );
    }
    this.capacity = bytes;
  }

  /** Diagnostic snapshot for monitoring. */
  diagnostics(): {
    capacityBytes: number;
    inUseBytes: number;
    droppedBudget: number;
  } {
    return {
      capacityBytes: this.capacity,
      inUseBytes: this.inUse,
      droppedBudget: this.droppedBudget,
    };
  }
}

/**
 * Module-wide singleton. Every call to {@link teeResponseForTokens}
 * goes through this instance. Tests reset via
 * {@link resetStreamCaptureBudgetForTests}; operators tune via
 * {@link setStreamCaptureBudgetCapacity}.
 */
export const streamCaptureBudget = new StreamCaptureBudget(
  DEFAULT_STREAM_CAPTURE_BUDGET_BYTES,
);

/** Operator hook: change the process-wide stream-capture memory cap. */
export function setStreamCaptureBudgetCapacity(bytes: number): void {
  streamCaptureBudget.setCapacity(bytes);
}

/** Diagnostic snapshot of the singleton. */
export function streamCaptureDiagnostics(): {
  capacityBytes: number;
  inUseBytes: number;
  droppedBudget: number;
} {
  return streamCaptureBudget.diagnostics();
}

/** Reset the singleton to defaults. Tests only. */
export function resetStreamCaptureBudgetForTests(): void {
  streamCaptureBudget.setCapacity(DEFAULT_STREAM_CAPTURE_BUDGET_BYTES);
  // Internals: zero counters by replacing the in-use accumulator. The
  // simplest correct approach is to release everything and re-create
  // the dropped counter — there's no public setter for those.
  const diag = streamCaptureBudget.diagnostics();
  streamCaptureBudget.release(diag.inUseBytes);
  // `droppedBudget` is monotonic; tests read the delta around their
  // own actions rather than expecting an absolute zero.
}

/**
 * Consume one half of a teed SSE stream and emit a final telemetry
 * event with input/output token counts. Returns when the stream ends.
 *
 * Abandonment handling: if the stream ends without its terminal usage
 * frame (client disconnect, truncation, buffer/budget abort) the event
 * carries no token attributes and is flagged `pricing_status =
 * "untallied"`. The tap never estimates — it reports only what the
 * provider's terminal frame actually stated.
 */
export async function captureStreamTokens(
  stream: ReadableStream<Uint8Array>,
  opts: CaptureOptions,
): Promise<void> {
  const reader = stream.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  let eventName = "message";
  let dataLines: string[] = [];
  const state = newUsageState();
  // `complete` flips false the moment the read loop bails early (buffer
  // overflow / hostile upstream). A clean `done` leaves it true; the
  // terminal-frame check below still decides tallied-vs-untallied.
  let complete = true;

  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      if (buffer.length > MAX_STREAM_EVENT_BYTES) {
        opts.logger?.warn("stream capture aborted: buffer exceeds limit", {
          limit: MAX_STREAM_EVENT_BYTES,
        });
        complete = false;
        break;
      }
      let boundary = buffer.indexOf("\n");
      while (boundary !== -1) {
        let line = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 1);
        if (line.endsWith("\r")) line = line.slice(0, -1);
        if (line.length === 0) {
          // Dispatch buffered event
          if (dataLines.length > 0) {
            applyFrame(opts.vendor, eventName, dataLines.join("\n"), state);
          }
          eventName = "message";
          dataLines = [];
        } else if (line.startsWith(":")) {
          // comment line
        } else if (line.startsWith("event:")) {
          eventName = line.slice(6).trimStart();
        } else if (line.startsWith("data:")) {
          dataLines.push(line.slice(5).trimStart());
        }
        boundary = buffer.indexOf("\n");
      }
    }
    // Dispatch any trailing event that wasn't terminated by a blank line.
    if (dataLines.length > 0) {
      applyFrame(opts.vendor, eventName, dataLines.join("\n"), state);
    }
  } catch (err) {
    // A read error mid-stream is an abandonment, not a tally. Surface
    // the partial as untallied rather than letting whatever counts we
    // saw masquerade as final.
    complete = false;
    opts.logger?.debug("stream capture read error", { err });
  } finally {
    try {
      reader.releaseLock();
    } catch {
      // no-op
    }
  }

  const { usageAttrs, pricingStatus } = finalizeUsage(state, complete);

  const event: TelemetryEvent = {
    event_type: "stream_completion",
    request_id: opts.requestId,
    agent_id: opts.agentId,
    method: opts.method,
    url: opts.url,
    vendor: opts.vendor,
    finish_reason: state.finishReason,
    latency_ms: Math.max(0, Date.now() - opts.startMs),
  };
  // Token attributes are emitted ONLY for a tallied stream. An
  // abandoned stream carries no estimate; it is flagged instead. Both
  // flat (`input_tokens`) and OTel-dotted (`gen_ai.usage.*`) keys are
  // attached so downstream flatten/semconv paths each find their shape.
  // The flat fields mirror the dotted attrs — i.e. the Anthropic-
  // normalized inclusive input, NOT the raw cache-exclusive count — so
  // the two never disagree about the billed total.
  if (pricingStatus === undefined) {
    event.input_tokens = usageAttrs[ATTR_INPUT_TOKENS] ?? null;
    event.output_tokens = usageAttrs[ATTR_OUTPUT_TOKENS] ?? null;
    Object.assign(event, usageAttrs);
  } else {
    event.input_tokens = null;
    event.output_tokens = null;
    event.pricing_status = pricingStatus;
  }
  opts.sink.enqueue(event);
}

/**
 * Pure usage tap, driveable from raw SSE frame strings.
 *
 * Feeds `frames` (each a complete SSE wire frame, in order — exactly
 * what the live tap sees off the socket) through the same vendor state
 * machine `captureStreamTokens` uses, then returns the OTel usage
 * attributes (Anthropic normalized to inclusive input) plus the
 * pricing status.
 *
 * `complete === false` forces `untallied` even if a terminal frame is
 * present, modelling a caller that observed a hard truncation. When
 * `complete` is true the result is tallied iff the terminal usage frame
 * arrived. Never throws on malformed frames — bad JSON is skipped.
 *
 * This is the fixture-testable seam (`schemas/genai-fixtures/streaming/`):
 * both SDKs must produce identical `usageAttrs` / `pricingStatus`
 * frame-for-frame.
 */
export function captureUsageFromFrames(
  vendor: StreamVendor,
  frames: readonly string[],
  complete: boolean,
): StreamCaptureResult {
  const state = newUsageState();
  for (const frame of frames) {
    applyRawFrame(vendor, frame, state);
  }
  return finalizeUsage(state, complete);
}

/**
 * Parse one raw SSE frame (possibly multi-line, with an `event:` tag and
 * one or more `data:` lines) and fold it into `state`. Tolerant of CRLF,
 * comment lines, and the OpenAI `[DONE]` sentinel. Never throws.
 */
function applyRawFrame(
  vendor: StreamVendor,
  frame: string,
  state: StreamUsageState,
): void {
  let eventName = "message";
  const dataLines: string[] = [];
  for (let line of frame.split("\n")) {
    if (line.endsWith("\r")) line = line.slice(0, -1);
    if (line.length === 0) {
      // Blank line = event boundary. Flush what we have, reset.
      if (dataLines.length > 0) {
        applyFrame(vendor, eventName, dataLines.join("\n"), state);
        dataLines.length = 0;
      }
      eventName = "message";
    } else if (line.startsWith(":")) {
      // comment
    } else if (line.startsWith("event:")) {
      eventName = line.slice(6).trimStart();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trimStart());
    }
  }
  if (dataLines.length > 0) {
    applyFrame(vendor, eventName, dataLines.join("\n"), state);
  }
}

/**
 * Fold one dispatched SSE event (its name + joined `data` payload) into
 * the usage accumulator. The `[DONE]` sentinel is a no-op; malformed
 * JSON is skipped. Shared by the live reader and the pure frame driver
 * so both paths produce identical state.
 */
function applyFrame(
  vendor: StreamVendor,
  eventName: string,
  payload: string,
  state: StreamUsageState,
): void {
  if (payload === "[DONE]") return;
  let parsed: unknown;
  try {
    parsed = JSON.parse(payload);
  } catch {
    return;
  }
  if (vendor === "openai") {
    applyOpenAIEvent(parsed, state);
  } else if (vendor === "anthropic") {
    applyAnthropicEvent(eventName, parsed, state);
  }
}

function applyOpenAIEvent(payload: unknown, state: StreamUsageState): void {
  if (!isPlainObject(payload)) return;
  const u = payload.usage;
  if (isPlainObject(u)) {
    // The presence of a `usage` object on an OpenAI chunk is the
    // terminal `include_usage` frame — what makes the stream tallied.
    const input = asInt(u.prompt_tokens) ?? asInt(u.input_tokens);
    if (input !== undefined) state.rawInput = input;
    const output = asInt(u.completion_tokens) ?? asInt(u.output_tokens);
    if (output !== undefined) state.output = output;
    // Detail counters are native-inclusive (cached ⊆ prompt,
    // reasoning ⊆ completion) — emitted as-is, no normalization.
    const promptDetails = asObject(u.prompt_tokens_details);
    if (promptDetails !== null) {
      const cached = asInt(promptDetails.cached_tokens);
      if (cached !== undefined) state.cacheRead = cached;
    }
    const completionDetails = asObject(u.completion_tokens_details);
    if (completionDetails !== null) {
      const reasoning = asInt(completionDetails.reasoning_tokens);
      if (reasoning !== undefined) state.reasoning = reasoning;
    }
    state.terminalSeen = true;
  }
  const choices = payload.choices;
  if (Array.isArray(choices) && choices.length > 0) {
    const first: unknown = choices[0];
    if (isPlainObject(first) && typeof first.finish_reason === "string") {
      state.finishReason = first.finish_reason;
    }
  }
}

function applyAnthropicEvent(
  eventName: string,
  payload: unknown,
  state: StreamUsageState,
): void {
  if (!isPlainObject(payload)) return;
  if (eventName === "message_start") {
    // Input usage (cache-exclusive) + cache counters land here. This is
    // NOT the terminal frame — output is finalized later in
    // `message_delta`, so seeing only `message_start` leaves the stream
    // untallied.
    const message = payload.message;
    if (isPlainObject(message)) {
      const u = message.usage;
      if (isPlainObject(u)) {
        const input = asInt(u.input_tokens);
        if (input !== undefined) {
          state.rawInput = input;
          // Anthropic's input_tokens excludes cache — normalize at finalize.
          state.inputExcludesCache = true;
        }
        const cacheRead = asInt(u.cache_read_input_tokens);
        if (cacheRead !== undefined) state.cacheRead = cacheRead;
        const cacheCreation = asInt(u.cache_creation_input_tokens);
        if (cacheCreation !== undefined) state.cacheCreation = cacheCreation;
      }
    }
  } else if (eventName === "message_delta") {
    const u = payload.usage;
    if (isPlainObject(u)) {
      const output = asInt(u.output_tokens);
      if (output !== undefined) {
        state.output = output;
        // The final `message_delta` carrying cumulative output is the
        // terminal usage frame for Anthropic.
        state.terminalSeen = true;
      }
    }
    const delta = payload.delta;
    if (isPlainObject(delta) && typeof delta.stop_reason === "string") {
      state.finishReason = delta.stop_reason;
    }
  }
}

/**
 * Collapse the accumulated state into the emitted usage attributes plus
 * pricing status, applying the Anthropic inclusive-input normalization.
 *
 * Tallied iff the terminal usage frame arrived AND the caller did not
 * signal a hard truncation (`complete === false`). An untallied stream
 * yields an empty `usageAttrs` and `pricingStatus = "untallied"` — the
 * engine never estimates.
 */
function finalizeUsage(
  state: StreamUsageState,
  complete: boolean,
): StreamCaptureResult {
  if (!complete || !state.terminalSeen) {
    return { usageAttrs: {}, pricingStatus: "untallied" };
  }
  const attrs: Record<string, number> = {};
  if (state.rawInput !== null) {
    // Anthropic's `input_tokens` EXCLUDES cache; normalize to the
    // inclusive total (input + cache_read + cache_creation) so the
    // invariant `cache_read + cache_creation <= input` holds and the
    // core's `settle_usage` netting reproduces the invoice — identical
    // to `_genai_body.ts`. OpenAI's `prompt_tokens` is ALREADY inclusive
    // (cached ⊆ prompt), so it passes through untouched. The
    // `inputExcludesCache` flag, set per vendor when the count is read,
    // is what keeps OpenAI from double-counting its cached tokens.
    attrs[ATTR_INPUT_TOKENS] = state.inputExcludesCache
      ? state.rawInput + (state.cacheRead ?? 0) + (state.cacheCreation ?? 0)
      : state.rawInput;
  }
  if (state.output !== null) {
    attrs[ATTR_OUTPUT_TOKENS] = state.output;
  }
  if (state.cacheRead !== null) {
    attrs[ATTR_CACHE_READ] = state.cacheRead;
  }
  if (state.cacheCreation !== null) {
    attrs[ATTR_CACHE_CREATION] = state.cacheCreation;
  }
  if (state.reasoning !== null) {
    attrs[ATTR_REASONING] = state.reasoning;
  }
  return { usageAttrs: attrs };
}

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/** Read a nested `Record`, else `null`. Mirrors `_genai_body.ts`. */
function asObject(value: unknown): Record<string, unknown> | null {
  if (value !== null && typeof value === "object" && !Array.isArray(value)) {
    return value as Record<string, unknown>;
  }
  return null;
}

/**
 * Return the value if it is a JSON integer, else `undefined`. Matches
 * `_genai_body.ts`'s `asInt` — booleans are excluded (not `number`),
 * floats/strings/NaN are skipped rather than coerced.
 */
function asInt(value: unknown): number | undefined {
  if (typeof value === "number" && Number.isInteger(value)) {
    return value;
  }
  return undefined;
}

/** Classify a request URL to pick the right SSE parser. */
export function vendorForUrl(url: string): StreamVendor {
  const u = url.toLowerCase();
  if (u.includes("api.openai.com") || u.includes("openai.azure.com")) return "openai";
  if (u.includes("api.anthropic.com")) return "anthropic";
  return "unknown";
}
