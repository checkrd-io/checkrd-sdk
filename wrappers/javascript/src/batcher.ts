/**
 * Telemetry batcher — buffers events, signs the batch with the WASM
 * engine (when keys are present), and ships to the control plane at
 * `/v1/telemetry`. Mirrors `wrappers/python/src/checkrd/batcher.py`.
 *
 * Design constraints:
 *
 *   - Non-blocking enqueue. The transport's hot path cannot afford to
 *     wait on any network I/O, signing, or serialization. `enqueue()`
 *     must be synchronous and O(1).
 *   - Back-pressure via counters, not dropping. When the queue fills
 *     past `maxQueueSize`, we drop the *newest* event (LIFO) and bump
 *     `droppedBackpressure`. Sentry's pattern.
 *   - Signed batches include RFC 9421 + RFC 9530 headers. Unsigned
 *     fallback exists for anonymous/KMS-only agents.
 *   - Retries + idempotency handled by {@link fetchWithRetry}. Every
 *     POST carries its own `Idempotency-Key`.
 *   - Graceful shutdown: `stop()` flushes pending events, waits up to
 *     `shutdownTimeoutMs`, then returns even if in-flight retries are
 *     still going. Parallel to Python's `TelemetryBatcher.stop()`.
 */

import { defaultControlHeaders, fetchWithRetry } from "./_retry.js";
import { APIUserAbortError } from "./exceptions.js";
import type { Logger } from "./_logger.js";
import type { WasmEngine } from "./engine.js";
import { CircuitBreaker, type CircuitBreakerDiagnostics } from "./_circuit_breaker.js";

/** Parse an RFC 6585 integer header, tolerating absent / non-numeric values. */
function parseIntHeader(value: string | null): number | null {
  if (value === null) return null;
  const n = Number.parseInt(value, 10);
  return Number.isFinite(n) ? n : null;
}

/** Clamp a sampling rate to `[0, 1]`; return `1` for NaN / undefined. */
function clampRate(raw: number | undefined): number {
  if (raw === undefined || !Number.isFinite(raw)) return 1;
  if (raw < 0) return 0;
  if (raw > 1) return 1;
  return raw;
}

/**
 * Current process id when running on Node (or Bun / Deno with Node
 * shim); `0` everywhere else. The fork-detection path only matters on
 * Node-shaped runtimes anyway — edge runtimes don't fork.
 */
function currentPid(): number {
  const proc = (globalThis as unknown as { process?: { pid?: number } }).process;
  return proc?.pid ?? 0;
}

/**
 * Heuristic: is this telemetry event an `allowed` decision? We look
 * for the two shapes the SDK emits — the WASM-decorated event
 * (`allowed: true`) and the vendor stream-completion event (which is
 * always attached to an already-allowed request). Denied events are
 * never sampled.
 */
function isAllowedEvent(event: TelemetryEvent): boolean {
  const allowed = (event as { allowed?: unknown }).allowed;
  if (allowed === false) return false;
  const deny = (event as { deny_reason?: unknown }).deny_reason;
  if (typeof deny === "string" && deny.length > 0) return false;
  return true;
}

/**
 * Construct a fresh W3C Trace Context `traceparent` header value.
 * Format: `00-<32hex trace-id>-<16hex parent-id>-01`. The control-plane
 * ingestion path picks this up and threads it through SQS into
 * ClickHouse so one trace spans SDK → writer.
 */
function newTraceparent(): string {
  const traceBytes = new Uint8Array(16);
  const parentBytes = new Uint8Array(8);
  globalThis.crypto.getRandomValues(traceBytes);
  globalThis.crypto.getRandomValues(parentBytes);
  const hex = (bytes: Uint8Array): string =>
    Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  return `00-${hex(traceBytes)}-${hex(parentBytes)}-01`;
}

/**
 * Hard ceiling on serialized body bytes for {@link TelemetryBatcher.urgentFlush}.
 *
 * The Fetch API spec caps the SUM of all in-flight `keepalive` request
 * bodies at 64 KiB per process. We use 60 KiB to leave headroom for the
 * RFC 9421 + RFC 9530 signature headers and any concurrent control-
 * plane PUT (e.g., a public-key registration also using keepalive).
 *
 * Public so the browser-flush helper and tests can refer to the same
 * value the batcher enforces.
 */
export const URGENT_FLUSH_BODY_LIMIT_BYTES = 60 * 1024;

/** An event queued for batched delivery. Opaque JSON dict. */
export type TelemetryEvent = Record<string, unknown>;

/**
 * Hint metadata passed to {@link BeforeSendHook}. Populated by the SDK
 * — users read but don't mutate. Mirrors Sentry's ``hint`` argument:
 * gives the hook context that's not stored in the event itself.
 *
 * Documented keys (additive — minor releases may add more, callers
 * should ``hint.agent_id ?? "unknown"`` rather than assume keys exist):
 *
 *   - ``agent_id``: the agent emitting the event.
 *   - ``event_kind``: ``"request_evaluation"`` for the policy-decision
 *     event the transport enqueues, ``"stream_completion"`` for the
 *     post-stream token-usage event, etc.
 */
export interface BeforeSendHint {
  agent_id: string;
  event_kind: string;
}

/**
 * Telemetry-event mutation/drop hook. Fires once per event right
 * before it's enqueued for batched delivery. Use it to:
 *
 *   - **redact fields**: ``return { ...event, body_hash: undefined }``
 *   - **drop events**: ``return null`` (event never ships; no
 *     ``dropped_*`` counter increments — operator-intended drops are
 *     not failures)
 *   - **transform payloads**: rewrite endpoint URLs, normalize status
 *     codes, attach static labels, etc.
 *
 * Same name and contract as Sentry's ``beforeSend``: returning ``null``
 * drops, returning the (possibly mutated) event ships it. Synchronous
 * — runs on the request critical path and must return promptly.
 *
 * Hook exceptions are caught and logged; a crashing hook drops the
 * event but never takes down the calling code.
 */
export type BeforeSendHook = (
  event: TelemetryEvent,
  hint: BeforeSendHint,
) => TelemetryEvent | null;

/** Options for {@link TelemetryBatcher}. All time fields are milliseconds. */
export interface BatcherOptions {
  /** Control-plane base URL (e.g., `https://api.checkrd.io`). */
  controlPlaneUrl: string;
  /** Control-plane API key. */
  apiKey: string;
  /** Agent ID, used for the X-Checkrd-Signer-Agent header when signing. */
  agentId: string;
  /** WASM engine used for optional batch signing. Omit for unsigned. */
  engine?: WasmEngine | undefined;
  /** Flush when queue reaches this many events. Default: 100. */
  batchSize?: number;
  /** Flush every N ms regardless of queue size. Default: 5000. */
  flushIntervalMs?: number;
  /** Drop events past this queue depth. Default: 10_000. */
  maxQueueSize?: number;
  /** Max attempts per batch POST. Default: 3. */
  maxAttempts?: number;
  /** Per-attempt HTTP timeout. Default: 30_000. */
  timeoutMs?: number;
  /** Stop-flushing ceiling. Default: 5_000. */
  shutdownTimeoutMs?: number;
  /** Fetch implementation. Default: `globalThis.fetch`. */
  fetch?: typeof fetch;
  /** Logger sink. Default: noop. */
  logger?: Logger;
  /** Signature validity window in seconds. Default: 300. */
  signatureValiditySecs?: number;
  /** API version pin (Stripe-style). Sent as `Checkrd-Version` when set. */
  apiVersion?: string;
  /**
   * Fraction of allowed events to forward, in `[0, 1]`. Denied events
   * always forward regardless. Default `1.0`.
   */
  samplingRate?: number;
  /** Circuit breaker shared with other control-plane clients (optional). */
  circuitBreaker?: CircuitBreaker;
  /**
   * Throttle interval for the backpressure-drop warning. The first drop
   * after a quiet period emits immediately; subsequent drops within
   * this window are suppressed. Default: 60_000 ms (one minute).
   *
   * Set lower in tests to assert the throttle behavior; raise in prod
   * to reduce log volume on chronic backpressure.
   */
  backpressureWarnIntervalMs?: number;
  /**
   * Synchronous mutation/drop hook. See {@link BeforeSendHook}.
   * Fires once per call to {@link TelemetryBatcher.enqueue} right
   * before the event lands in the queue.
   */
  beforeSend?: BeforeSendHook;
}

/** Diagnostic counters returned from {@link TelemetryBatcher.diagnostics}. */
export interface BatcherDiagnostics {
  /** Events successfully delivered (across all retries). */
  sent: number;
  /** Events dropped because the queue was at `maxQueueSize`. */
  droppedBackpressure: number;
  /** Events dropped because the HTTP call exhausted all retries. */
  droppedSendError: number;
  /** Events dropped before enqueue by the sampling rate. */
  droppedSampled: number;
  /** Events currently buffered, waiting for the next flush. */
  pending: number;
  /** True when background flushing is active. */
  running: boolean;
  /** Latest `ratelimit-remaining` seen on a control-plane response. */
  rateLimitRemaining: number | null;
  /** Latest `ratelimit-reset` (Unix-seconds) seen on a control-plane response. */
  rateLimitResetAt: number | null;
  /**
   * Most recent ``Checkrd-Request-Id`` (or ``X-Request-Id``) seen on a
   * successful or failed control-plane response. Surfaced for support
   * tickets and dashboards — paste this into a Checkrd ticket and the
   * on-call can locate the exact server-side request, the same Stripe
   * convention every observability dashboard expects. ``null`` when no
   * batch has been sent yet in this process.
   */
  lastRequestId: string | null;
  /** Circuit-breaker state for the telemetry endpoint. */
  circuitBreaker: CircuitBreakerDiagnostics;
}

/**
 * Background batcher. Safe to create per-process — each owns its own
 * timer and queue. Use {@link TelemetryBatcher.stop} on SIGTERM to
 * flush pending events.
 */
export class TelemetryBatcher {
  private readonly controlPlaneUrl: string;
  private readonly apiKey: string;
  private readonly agentId: string;
  private readonly engine: WasmEngine | undefined;
  private readonly batchSize: number;
  private readonly flushIntervalMs: number;
  private readonly maxQueueSize: number;
  private readonly maxAttempts: number;
  private readonly timeoutMs: number;
  private readonly shutdownTimeoutMs: number;
  private readonly fetchImpl: typeof fetch;
  private readonly logger: Logger | undefined;
  private readonly signatureValiditySecs: number;
  private readonly apiVersion: string;
  private readonly samplingRate: number;
  private readonly circuitBreaker: CircuitBreaker;
  private readonly backpressureWarnIntervalMs: number;
  private readonly beforeSend: BeforeSendHook | undefined;
  private parentPid: number;

  private queue: TelemetryEvent[] = [];
  private flushInFlight: Promise<void> | null = null;
  private timer: ReturnType<typeof setInterval> | null = null;
  private running = false;
  private stopping = false;

  private sent = 0;
  private droppedBackpressure = 0;
  private droppedSendError = 0;
  private droppedSampled = 0;
  private rateLimitRemaining: number | null = null;
  private rateLimitResetAt: number | null = null;
  private lastRequestId: string | null = null;
  private lastBackpressureWarnAt = 0;

  constructor(opts: BatcherOptions) {
    this.controlPlaneUrl = opts.controlPlaneUrl.replace(/\/$/, "");
    this.apiKey = opts.apiKey;
    this.agentId = opts.agentId;
    this.engine = opts.engine;
    this.batchSize = opts.batchSize ?? 100;
    this.flushIntervalMs = opts.flushIntervalMs ?? 5_000;
    this.maxQueueSize = opts.maxQueueSize ?? 10_000;
    this.maxAttempts = opts.maxAttempts ?? 3;
    this.timeoutMs = opts.timeoutMs ?? 30_000;
    this.shutdownTimeoutMs = opts.shutdownTimeoutMs ?? 5_000;
    this.fetchImpl = opts.fetch ?? globalThis.fetch.bind(globalThis);
    this.logger = opts.logger;
    this.signatureValiditySecs = opts.signatureValiditySecs ?? 300;
    this.apiVersion = opts.apiVersion ?? "";
    this.samplingRate = clampRate(opts.samplingRate);
    this.circuitBreaker = opts.circuitBreaker ?? new CircuitBreaker();
    this.backpressureWarnIntervalMs = opts.backpressureWarnIntervalMs ?? 60_000;
    this.beforeSend = opts.beforeSend;
    this.parentPid = currentPid();
  }

  /**
   * Start the background timer. Safe to call multiple times — a second
   * call is a no-op while already running.
   */
  start(): void {
    if (this.running) return;
    this.running = true;
    this.stopping = false;
    this.timer = setInterval(() => {
      if (this.queue.length > 0) {
        void this.flush().catch((err: unknown) => {
          this.logger?.error("telemetry flush failed", { err });
        });
      }
    }, this.flushIntervalMs);
    // In Node, don't keep the process alive just because of this timer.
    // The presence of `unref` is how we detect we're on Node; it's a no-op
    // on runtimes where the method doesn't exist (Cloudflare Workers etc).
    const nodeTimer = this.timer as { unref?: () => void };
    if (typeof nodeTimer.unref === "function") nodeTimer.unref();
  }

  /**
   * Buffer an event for the next batch. Non-blocking. When the queue is
   * at `maxQueueSize`, the event is dropped and `droppedBackpressure`
   * is incremented.
   *
   * Sampling: when `samplingRate < 1` and the event is an *allowed*
   * decision, it is subject to the random-drop filter. Denied events
   * always pass through — the policy audit trail must not sample.
   */
  enqueue(event: TelemetryEvent): void {
    this.maybeResetAfterFork();
    if (this.stopping) {
      this.droppedBackpressure += 1;
      this.warnBackpressureThrottled();
      return;
    }
    if (this.beforeSend !== undefined) {
      let mutated: TelemetryEvent | null;
      try {
        const hint: BeforeSendHint = {
          agent_id: this.agentId,
          event_kind:
            (event.event_type as string | undefined) ?? "request_evaluation",
        };
        mutated = this.beforeSend(event, hint);
      } catch (err) {
        // Hook exceptions never crash the caller — that's the entire
        // point of an async-safe extension surface. Log and treat
        // as a drop (operator's hook is buggy; their problem to
        // diagnose, not a reason to take down the request path).
        this.logger?.error("checkrd: beforeSend hook threw; dropping event", {
          err,
        });
        return;
      }
      if (mutated === null) {
        // Operator chose to drop. Not a failure — don't increment
        // ``droppedBackpressure`` / ``droppedSendError``.
        return;
      }
      event = mutated;
    }
    if (this.samplingRate < 1 && isAllowedEvent(event)) {
      if (Math.random() >= this.samplingRate) {
        this.droppedSampled += 1;
        return;
      }
    }
    if (this.queue.length >= this.maxQueueSize) {
      this.droppedBackpressure += 1;
      this.warnBackpressureThrottled();
      return;
    }
    this.queue.push(event);
    if (this.queue.length >= this.batchSize) {
      void this.flush().catch((err: unknown) => {
        this.logger?.error("telemetry flush failed", { err });
      });
    }
  }

  /**
   * Emit a throttled `warn` when backpressure has caused an event drop.
   * Without this, telemetry loss is invisible to operators until they
   * actively query `.diagnostics()`. Throttled to once per 60 s so a
   * sustained burst of drops doesn't flood the logger.
   */
  private warnBackpressureThrottled(): void {
    const now = Date.now();
    if (now - this.lastBackpressureWarnAt < this.backpressureWarnIntervalMs) {
      return;
    }
    this.lastBackpressureWarnAt = now;
    this.logger?.warn(
      "checkrd: telemetry events being dropped due to backpressure",
      {
        droppedBackpressure: this.droppedBackpressure,
        maxQueueSize: this.maxQueueSize,
        hint:
          "raise maxQueueSize, lower samplingRate, or investigate " +
          "control-plane latency",
      },
    );
  }

  /**
   * Detect when the host process has been forked (Node's `cluster.fork`
   * or `child_process.fork`). The parent's queue and timer state are
   * meaningless in the child; reset them so the child starts clean
   * rather than inheriting half-flushed state.
   */
  private maybeResetAfterFork(): void {
    const pid = currentPid();
    if (pid === this.parentPid) return;
    this.parentPid = pid;
    this.queue = [];
    this.sent = 0;
    this.droppedBackpressure = 0;
    this.droppedSendError = 0;
    this.droppedSampled = 0;
    this.lastBackpressureWarnAt = 0;
    this.flushInFlight = null;
    // Timer IDs don't cross the fork boundary meaningfully; restart if
    // we had one.
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
      if (this.running) this.start();
    }
    this.logger?.debug("telemetry batcher reset after process fork", { pid });
  }

  /**
   * Flush the current queue synchronously. Awaits the network call. Safe
   * to call any time; when there's already an in-flight flush, this
   * returns the same promise so callers never have overlapping POSTs.
   */
  flush(): Promise<void> {
    if (this.flushInFlight) return this.flushInFlight;
    if (this.queue.length === 0) return Promise.resolve();

    const batch = this.queue;
    this.queue = [];

    this.flushInFlight = this.sendBatch(batch).finally(() => {
      this.flushInFlight = null;
    });
    return this.flushInFlight;
  }

  /**
   * Stop background flushing. Drains the queue once with a bounded wait,
   * then resolves. Safe to call multiple times.
   */
  async stop(): Promise<void> {
    if (this.stopping) {
      if (this.flushInFlight) await this.flushInFlight;
      return;
    }
    this.stopping = true;
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
    // Try to drain once. Respect the shutdown budget so we don't block
    // SIGTERM indefinitely when the control plane is unresponsive.
    try {
      await this.withTimeout(this.flush(), this.shutdownTimeoutMs);
    } catch (err) {
      this.logger?.warn("telemetry shutdown flush timed out", { err });
      this.droppedSendError += this.queue.length;
      this.queue = [];
    }
    this.running = false;
  }

  /**
   * Synchronous best-effort flush for unload contexts.
   *
   * Drains the queue, signs, and fires `fetch(url, { keepalive: true })`
   * **without awaiting the response**. Intended for `pagehide` /
   * `beforeunload` handlers in browsers and SIGTERM handlers in Node —
   * any place where the event loop is about to disappear and the
   * normal {@link flush} would lose its in-flight Promise.
   *
   * Why not `navigator.sendBeacon`?  The Beacon API ships POSTs but
   * cannot set custom headers — the Checkrd protocol carries
   * `Content-Digest`, `Signature`, and `Signature-Input` (RFC 9421 +
   * RFC 9530) plus `X-Checkrd-Signer-Agent`, none of which Beacon
   * permits. `fetch(..., { keepalive: true })` preserves header
   * semantics at the same delivery guarantee, and modern browsers
   * (Chrome 66+, Firefox 66+, Safari 13+) ship the request even after
   * page navigation.
   *
   * Constraints:
   *   - The Fetch API caps keepalive request bodies at 64 KiB *across
   *     the entire process*. We trim to {@link URGENT_FLUSH_BODY_LIMIT_BYTES}
   *     by dropping the OLDEST events first (FIFO) so the most recent
   *     activity — usually the part the operator most cares about —
   *     survives an unload.
   *   - Signing is synchronous (WASM FFI is sync), so a missing engine
   *     key on this path produces a structured drop and no request is
   *     fired. Mirrors `sendBatch`'s fail-closed behavior.
   *   - No retries: under unload there is no time for them. Failures
   *     are silent — there is no error channel left to read.
   */
  urgentFlush(): void {
    if (this.queue.length === 0) return;
    if (this.stopping) return;
    const batch = this.queue;
    this.queue = [];

    // Trim to fit keepalive budget. Serialize once to measure, drop
    // oldest events (FIFO) until under cap, then serialize the trimmed
    // form for the actual ship. For the common case (small batches),
    // the second serialize is a no-op miss — we keep the first result
    // when nothing was trimmed.
    let bodyJson = JSON.stringify(batch);
    let bodyBytes = new TextEncoder().encode(bodyJson);
    if (bodyBytes.byteLength > URGENT_FLUSH_BODY_LIMIT_BYTES) {
      while (
        batch.length > 1 &&
        bodyBytes.byteLength > URGENT_FLUSH_BODY_LIMIT_BYTES
      ) {
        // FIFO drop — the oldest event leaves first. Each pop is O(n)
        // because Array.shift is, but the loop bound is small and this
        // path runs at most once per page lifetime.
        batch.shift();
        this.droppedSendError += 1;
        bodyJson = JSON.stringify(batch);
        bodyBytes = new TextEncoder().encode(bodyJson);
      }
      this.logger?.warn(
        "telemetry urgent flush trimmed oldest events to fit keepalive budget",
        { kept: batch.length, limitBytes: URGENT_FLUSH_BODY_LIMIT_BYTES },
      );
    }
    if (batch.length === 0 || bodyBytes.byteLength === 0) return;

    const targetUri = `${this.controlPlaneUrl}/v1/telemetry`;
    const headers: Record<string, string> = {
      ...defaultControlHeaders(this.apiKey, { apiVersion: this.apiVersion }),
      traceparent: this.newTraceparentSync(),
    };

    let signed: ReturnType<WasmEngine["signTelemetryBatch"]> | null = null;
    try {
      signed = this.maybeSign(bodyBytes, targetUri);
    } catch (err) {
      this.droppedSendError += batch.length;
      this.logger?.error(
        "telemetry urgent flush signing failed; dropping batch rather than sending unsigned",
        { count: batch.length, err },
      );
      return;
    }
    if (signed) {
      headers["Content-Digest"] = signed.content_digest;
      headers["Signature-Input"] = signed.signature_input;
      headers.Signature = signed.signature;
      headers["X-Checkrd-Signer-Agent"] = this.agentId;
      headers["X-Checkrd-DSSE-Envelope"] = signed.dsse_envelope;
      headers["X-Checkrd-Instance-Id"] = signed.instance_id;
    }

    // Fire-and-forget. The browser keeps the request alive past
    // navigation thanks to `keepalive: true`. We attach an
    // unhandled-rejection guard so a network failure on the unload
    // path does not surface as `unhandledrejection` in the console.
    try {
      const promise = this.fetchImpl(targetUri, {
        method: "POST",
        headers,
        body: bodyJson,
        keepalive: true,
      });
      // `void` casts the Promise to a discarded expression so the
      // type checker stops complaining; `.catch` swallows rejection
      // so we don't pollute the global unhandledrejection event.
      void promise.then(
        (response) => {
          this.sent += batch.length;
          // Capture request-id even on the unload path — operators
          // who check ``diagnostics()`` immediately after page reload
          // (e.g., dev tools) still get the correlation token from
          // the last in-flight batch.
          this.lastRequestId =
            response.headers.get("checkrd-request-id") ??
            response.headers.get("x-request-id");
        },
        () => {
          this.droppedSendError += batch.length;
        },
      );
    } catch (err) {
      // Synchronous throw from `fetch` (extremely rare — typically
      // only when `keepalive` isn't supported on this runtime). Treat
      // it as a drop and move on; no point lecturing the unload path.
      this.droppedSendError += batch.length;
      this.logger?.warn("telemetry urgent flush threw synchronously", { err });
    }
  }

  /**
   * Generate a fresh W3C traceparent. Matches the private helper in
   * `_send` so urgent flushes stamp the same shape. Keeping this
   * inline (instead of factoring out) avoids cross-method coupling on
   * a hot, simple operation.
   */
  private newTraceparentSync(): string {
    return newTraceparent();
  }

  /** Snapshot of delivery counters, mirroring Python's `diagnostics()`. */
  diagnostics(): BatcherDiagnostics {
    return {
      sent: this.sent,
      droppedBackpressure: this.droppedBackpressure,
      droppedSendError: this.droppedSendError,
      droppedSampled: this.droppedSampled,
      pending: this.queue.length,
      running: this.running,
      rateLimitRemaining: this.rateLimitRemaining,
      rateLimitResetAt: this.rateLimitResetAt,
      lastRequestId: this.lastRequestId,
      circuitBreaker: this.circuitBreaker.diagnostics(),
    };
  }

  // -------------------------------------------------------------------
  // Internals
  // -------------------------------------------------------------------

  private async sendBatch(batch: TelemetryEvent[]): Promise<void> {
    if (batch.length === 0) return;
    const bodyJson = JSON.stringify(batch);
    const bodyBytes = new TextEncoder().encode(bodyJson);
    const targetUri = `${this.controlPlaneUrl}/v1/telemetry`;
    const headers: Record<string, string> = {
      ...defaultControlHeaders(this.apiKey, { apiVersion: this.apiVersion }),
      // W3C Trace Context. Every batch carries its own traceparent so
      // the control-plane side can thread a single trace through the
      // ingestion → writer → ClickHouse pipeline. Stamped in the
      // batcher (not `defaultControlHeaders`) because only telemetry
      // carries an end-to-end trace — key registration and SSE reuse
      // the control-plane's own span.
      traceparent: newTraceparent(),
    };

    // Fail closed on signing errors: an engine that has been given a
    // private key MUST produce a signature. Falling through to unsigned
    // would let an attacker who can induce signing failures (via memory
    // pressure, corrupted state, or host compromise) trick the ingest
    // side into accepting events it would otherwise reject. Only the
    // truly-anonymous path (no engine attached) is permitted to skip.
    let signed: ReturnType<WasmEngine["signTelemetryBatch"]> | null;
    try {
      signed = this.maybeSign(bodyBytes, targetUri);
    } catch (err) {
      this.droppedSendError += batch.length;
      this.logger?.error(
        "telemetry signing failed; dropping batch rather than sending unsigned",
        { count: batch.length, err },
      );
      return;
    }
    if (signed) {
      headers["Content-Digest"] = signed.content_digest;
      headers["Signature-Input"] = signed.signature_input;
      headers.Signature = signed.signature;
      headers["X-Checkrd-Signer-Agent"] = this.agentId;
      headers["X-Checkrd-DSSE-Envelope"] = signed.dsse_envelope;
      headers["X-Checkrd-Instance-Id"] = signed.instance_id;
    }

    try {
      const response = await fetchWithRetry(
        targetUri,
        { method: "POST", headers, body: bodyJson },
        {
          fetch: this.fetchImpl,
          maxAttempts: this.maxAttempts,
          timeoutMs: this.timeoutMs,
          logger: this.logger,
          circuitBreaker: this.circuitBreaker,
        },
      );
      // Harvest RFC 6585-style rate-limit headers before draining.
      this.rateLimitRemaining = parseIntHeader(response.headers.get("ratelimit-remaining"));
      this.rateLimitResetAt = parseIntHeader(response.headers.get("ratelimit-reset"));
      // Capture the server-assigned request-id for support-ticket
      // correlation. Stripe / OpenAI / Anthropic all surface this; the
      // checkrd control plane echoes ``Checkrd-Request-Id`` and accepts
      // the conventional ``X-Request-Id`` form for cross-tooling reach.
      this.lastRequestId =
        response.headers.get("checkrd-request-id") ??
        response.headers.get("x-request-id");
      // Drain the body so the connection can be reused. Errors here are
      // harmless — the control plane sends back a tiny ack.
      try {
        await response.body?.cancel();
      } catch {
        // no-op
      }
      this.sent += batch.length;
      this.logger?.debug("telemetry batch delivered", { count: batch.length });
    } catch (err) {
      if (err instanceof APIUserAbortError) throw err;
      this.droppedSendError += batch.length;
      this.logger?.warn("telemetry batch dropped", {
        count: batch.length,
        err,
      });
    }
  }

  /**
   * Produce the signature envelope, or `null` only when the batcher was
   * constructed without an engine (intentional anonymous mode). Any
   * engine-present failure is re-thrown so `sendBatch` can fail closed.
   */
  private maybeSign(
    bodyBytes: Uint8Array,
    targetUri: string,
  ): ReturnType<WasmEngine["signTelemetryBatch"]> | null {
    if (!this.engine) return null;
    const created = Math.floor(Date.now() / 1000);
    const expires = created + this.signatureValiditySecs;
    const result = this.engine.signTelemetryBatch({
      batchJson: bodyBytes,
      targetUri,
      signerAgent: this.agentId,
      nonce: this.newNonce(),
      created,
      expires,
    });
    if (result === null) {
      throw new Error(
        "engine returned no signature; refusing to send unsigned telemetry",
      );
    }
    return result;
  }

  private newNonce(): string {
    const buf = new Uint8Array(16);
    globalThis.crypto.getRandomValues(buf);
    return Array.from(buf, (b) => b.toString(16).padStart(2, "0")).join("");
  }

  private withTimeout<T>(promise: Promise<T>, ms: number): Promise<T> {
    return new Promise<T>((resolve, reject) => {
      const timer = setTimeout(() => {
        reject(new Error(`timeout after ${ms.toString()}ms`));
      }, ms);
      promise.then(
        (v) => {
          clearTimeout(timer);
          resolve(v);
        },
        (err: unknown) => {
          clearTimeout(timer);
          reject(err instanceof Error ? err : new Error(String(err)));
        },
      );
    });
  }
}
