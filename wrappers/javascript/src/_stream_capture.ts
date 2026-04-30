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

/** Usage numbers extracted from a stream. */
export interface StreamUsage {
  input_tokens: number | null;
  output_tokens: number | null;
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
  const usage: StreamUsage = { input_tokens: null, output_tokens: null };
  let finishReason: string | null = null;

  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      if (buffer.length > MAX_STREAM_EVENT_BYTES) {
        opts.logger?.warn("stream capture aborted: buffer exceeds limit", {
          limit: MAX_STREAM_EVENT_BYTES,
        });
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
            const payload = dataLines.join("\n");
            if (payload !== "[DONE]") {
              applyEventToUsage(eventName, payload, opts.vendor, usage, (fr) => {
                finishReason = fr;
              });
            }
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
      const payload = dataLines.join("\n");
      if (payload !== "[DONE]") {
        applyEventToUsage(eventName, payload, opts.vendor, usage, (fr) => {
          finishReason = fr;
        });
      }
    }
  } finally {
    try {
      reader.releaseLock();
    } catch {
      // no-op
    }
  }

  const event: TelemetryEvent = {
    event_type: "stream_completion",
    request_id: opts.requestId,
    agent_id: opts.agentId,
    method: opts.method,
    url: opts.url,
    vendor: opts.vendor,
    input_tokens: usage.input_tokens,
    output_tokens: usage.output_tokens,
    finish_reason: finishReason,
    latency_ms: Math.max(0, Date.now() - opts.startMs),
  };
  opts.sink.enqueue(event);
}

function applyEventToUsage(
  eventName: string,
  payload: string,
  vendor: StreamVendor,
  usage: StreamUsage,
  setFinishReason: (reason: string) => void,
): void {
  let parsed: unknown;
  try {
    parsed = JSON.parse(payload);
  } catch {
    return;
  }
  if (vendor === "openai") {
    applyOpenAIEvent(parsed, usage, setFinishReason);
  } else if (vendor === "anthropic") {
    applyAnthropicEvent(eventName, parsed, usage, setFinishReason);
  }
}

function applyOpenAIEvent(
  payload: unknown,
  usage: StreamUsage,
  setFinishReason: (reason: string) => void,
): void {
  if (!isPlainObject(payload)) return;
  const u = payload.usage;
  if (isPlainObject(u)) {
    if (typeof u.prompt_tokens === "number") usage.input_tokens = u.prompt_tokens;
    if (typeof u.input_tokens === "number") usage.input_tokens = u.input_tokens;
    if (typeof u.completion_tokens === "number") usage.output_tokens = u.completion_tokens;
    if (typeof u.output_tokens === "number") usage.output_tokens = u.output_tokens;
  }
  const choices = payload.choices;
  if (Array.isArray(choices) && choices.length > 0) {
    const first: unknown = choices[0];
    if (isPlainObject(first) && typeof first.finish_reason === "string") {
      setFinishReason(first.finish_reason);
    }
  }
}

function applyAnthropicEvent(
  eventName: string,
  payload: unknown,
  usage: StreamUsage,
  setFinishReason: (reason: string) => void,
): void {
  if (!isPlainObject(payload)) return;
  if (eventName === "message_start") {
    const message = payload.message;
    if (isPlainObject(message)) {
      const u = message.usage;
      if (isPlainObject(u) && typeof u.input_tokens === "number") {
        usage.input_tokens = u.input_tokens;
      }
    }
  } else if (eventName === "message_delta") {
    const u = payload.usage;
    if (isPlainObject(u) && typeof u.output_tokens === "number") {
      usage.output_tokens = u.output_tokens;
    }
    const delta = payload.delta;
    if (isPlainObject(delta) && typeof delta.stop_reason === "string") {
      setFinishReason(delta.stop_reason);
    }
  }
}

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/** Classify a request URL to pick the right SSE parser. */
export function vendorForUrl(url: string): StreamVendor {
  const u = url.toLowerCase();
  if (u.includes("api.openai.com") || u.includes("openai.azure.com")) return "openai";
  if (u.includes("api.anthropic.com")) return "anthropic";
  return "unknown";
}
