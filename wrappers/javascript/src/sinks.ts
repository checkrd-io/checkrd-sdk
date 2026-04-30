/**
 * Pluggable telemetry sinks. Mirrors `wrappers/python/src/checkrd/sinks.py`.
 *
 * The SDK's default sink is {@link ControlPlaneSink} (backed by a
 * {@link TelemetryBatcher}). Operators deploying offline/air-gapped
 * (Tier 3) swap in {@link ConsoleSink}, {@link JsonFileSink}, or a
 * custom adapter that pipes to their own observability stack.
 *
 * The contract is intentionally narrow — `enqueue()` is non-blocking,
 * `close()` drains gracefully — so any bespoke destination (Kafka, S3,
 * Loki, etc.) can be wired in as a one-file adapter.
 */

import type { TelemetryBatcher, TelemetryEvent } from "./batcher.js";
import type { Logger } from "./_logger.js";
import { scrubTelemetryEvent } from "./_sensitive.js";

export type { TelemetryEvent } from "./batcher.js";

/** Narrow contract every telemetry sink must implement. */
export interface TelemetrySink {
  /** Accept an event. Must be non-blocking. */
  enqueue(event: TelemetryEvent): void;
  /** Flush + release resources. Safe to call multiple times. */
  close(): Promise<void>;
}

/**
 * Sink that forwards events through the given {@link TelemetryBatcher}.
 * This is the default production path.
 */
export class ControlPlaneSink implements TelemetrySink {
  constructor(private readonly batcher: TelemetryBatcher) {}

  enqueue(event: TelemetryEvent): void {
    this.batcher.enqueue(event);
  }

  async close(): Promise<void> {
    await this.batcher.stop();
  }
}

/** Options for {@link ConsoleSink}. */
export interface ConsoleSinkOptions {
  /** Logger to emit events through. Defaults to a console-backed logger at info level. */
  logger?: Logger;
  /** Log level each event fires at (`info` | `debug`). Default: `info`. */
  level?: "info" | "debug";
}

/**
 * Sink that logs each event through the provided {@link Logger}. Useful
 * for local development and air-gapped deployments wired to an existing
 * logging pipeline (pino → journald, for example).
 */
export class ConsoleSink implements TelemetrySink {
  private readonly logger: Logger;
  private readonly level: "info" | "debug";

  constructor(opts: ConsoleSinkOptions = {}) {
    this.logger = opts.logger ?? defaultConsoleLogger();
    this.level = opts.level ?? "info";
  }

  enqueue(event: TelemetryEvent): void {
    // Scrub before logging. A console sink in dev is the most common
    // path where an operator has eyes on telemetry; it's also the one
    // most likely to tail into a shared log aggregator. Redact at the
    // boundary so neither the eye nor the aggregator sees secrets.
    this.logger[this.level]("checkrd event", scrubTelemetryEvent(event));
  }

  async close(): Promise<void> {
    // nothing to do — console is already synchronous
    return Promise.resolve();
  }
}

/** Options for {@link JsonFileSink}. */
export interface JsonFileSinkOptions {
  /** Filesystem path to append JSON lines to. */
  path: string;
  /** Logger used for internal diagnostics. */
  logger?: Logger;
}

/**
 * Sink that appends each event as a JSON Lines record to a file.
 *
 * Node-only — imports `node:fs` lazily so non-Node runtimes (Cloudflare
 * Workers, Vercel Edge, browser) do not pay the import cost and do not
 * blow up when the sink is unused.
 */
export class JsonFileSink implements TelemetrySink {
  private readonly path: string;
  private readonly logger: Logger | undefined;
  private stream: { write: (chunk: string) => boolean; end: () => void } | null = null;
  private ready: Promise<void>;

  constructor(opts: JsonFileSinkOptions) {
    this.path = opts.path;
    this.logger = opts.logger;
    this.ready = this.openStream();
  }

  private async openStream(): Promise<void> {
    // Lazy import keeps this out of the non-Node bundle.
    const fs = await import("node:fs");
    this.stream = fs.createWriteStream(this.path, { flags: "a" });
  }

  enqueue(event: TelemetryEvent): void {
    // Writes are fire-and-forget. We serialize first so the caller never
    // observes a file-permission error on the hot path. Scrub BEFORE
    // serializing — a JSONL file is typically post-processed (grep,
    // jq, ship-to-S3) and redacted at rest means the blast radius of a
    // log leak is bounded.
    const line = `${JSON.stringify(scrubTelemetryEvent(event))}\n`;
    this.ready
      .then(() => {
        const stream = this.stream;
        if (stream === null) return;
        stream.write(line);
      })
      .catch((err: unknown) => {
        this.logger?.warn("JsonFileSink enqueue failed", { err });
      });
  }

  async close(): Promise<void> {
    await this.ready;
    const stream = this.stream;
    if (stream === null) return;
    await new Promise<void>((resolve) => {
      // Node's WriteStream.end() triggers a "finish" event when safe; we
      // resolve on process tick to be portable to polyfilled streams.
      try {
        stream.end();
      } finally {
        setImmediate(resolve);
      }
    });
    this.stream = null;
  }
}

/**
 * Composite sink — forwards each event to multiple underlying sinks.
 * Useful when a customer wants both control-plane ingestion and local
 * JSON Lines on disk for offline analysis.
 */
export class CompositeSink implements TelemetrySink {
  constructor(private readonly sinks: TelemetrySink[]) {}

  enqueue(event: TelemetryEvent): void {
    for (const sink of this.sinks) sink.enqueue(event);
  }

  async close(): Promise<void> {
    await Promise.allSettled(this.sinks.map((s) => s.close()));
  }
}

export { OtlpSink } from "./_otlp.js";
export type { OtlpSinkOptions } from "./_otlp.js";
export { OtelSpanSink } from "./_otel_span_sink.js";
export type { OtelSpanSinkOptions } from "./_otel_span_sink.js";

function defaultConsoleLogger(): Logger {
  return {
     
    debug: (msg, ...args) => { console.debug(msg, ...args); },
     
    info: (msg, ...args) => { console.info(msg, ...args); },
     
    warn: (msg, ...args) => { console.warn(msg, ...args); },
     
    error: (msg, ...args) => { console.error(msg, ...args); },
  };
}
