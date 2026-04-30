/**
 * SSE control receiver. Mirrors `wrappers/python/src/checkrd/control.py`.
 *
 * Subscribes to `GET /v1/agents/{id}/control` using `fetch` + a streaming
 * `ReadableStream` so the same code runs on Node, Bun, Deno, Cloudflare
 * Workers, Vercel Edge, and the browser — no `EventSource` polyfill
 * required. The runtime-agnostic reader is what makes this receiver
 * viable in edge environments where the Python SDK's `httpx-sse` would
 * be unavailable.
 *
 * On stream disconnect the receiver reconnects with exponential backoff
 * (1s → 60s), and while waiting for the next retry it polls
 * `GET /v1/agents/{id}/control/state` so the kill switch is still
 * applied if the SSE connection is stuck.
 */

import {
  handleControlEvent,
  type ControlEngine,
  type PolicyUpdateOptions,
} from "./control.js";
import type { CircuitBreaker } from "./_circuit_breaker.js";
import { APIUserAbortError } from "./exceptions.js";
import { platformHeaders } from "./_platform.js";
import { trustedPolicyKeysJson, warnIfMisconfigured } from "./_trust.js";
import { VERSION } from "./_version.js";
import type { Logger } from "./_logger.js";

/** Options for {@link ControlReceiver}. Time fields are milliseconds. */
export interface ReceiverOptions {
  /** Control-plane base URL, without trailing slash. */
  controlPlaneUrl: string;
  /** Control-plane API key for auth. */
  apiKey: string;
  /** Agent ID whose control stream to subscribe to. */
  agentId: string;
  /** Engine to dispatch events to. */
  engine: ControlEngine;
  /**
   * Stripe-style date pin sent as ``Checkrd-Version`` on the subscribe
   * and state-poll requests when non-empty. Pinning the control-plane
   * API shape from the SDK side protects against silent breaking
   * changes in long-running SSE connections.
   */
  apiVersion?: string | undefined;
  /** Logger sink. */
  logger?: Logger;
  /** Fetch implementation. Default: `globalThis.fetch`. */
  fetch?: typeof fetch;
  /** Initial reconnect delay on disconnect. Default: 1_000. */
  initialBackoffMs?: number;
  /** Max reconnect delay ceiling. Default: 60_000. */
  maxBackoffMs?: number;
  /** How often to run the polling fallback while reconnecting. Default: 15_000. */
  pollIntervalMs?: number;
  /**
   * Per-read SSE timeout in milliseconds. If no bytes arrive from the
   * control plane within this window, the stream is aborted and the
   * reconnect loop is triggered. Default: ``120_000`` (120 seconds),
   * chosen to comfortably exceed typical load-balancer idle timeouts
   * (AWS ALB: 60s, Cloudflare: 100s, GCP LB: 600s) so a single missed
   * heartbeat does not thrash the connection.
   *
   * **Set to ``0`` to disable the timeout entirely.** This is an
   * explicit opt-in for callers who know their control plane sends
   * heartbeats less frequently than 120s. With no timeout, a control
   * plane that stops responding (TCP zombie, half-open connection)
   * leaves the receiver hung and the kill switch unreachable — use
   * with care.
   */
  readTimeoutMs?: number;
  /**
   * Wires the DSSE-signed policy-bundle install path on
   * ``policy_updated`` SSE events. When omitted, the receiver
   * defaults to {@link trustedPolicyKeysJson} as the trust-list loader
   * and a 24-hour bundle freshness window — the same defaults the
   * Python SDK uses, so both wrappers verify against an identical key
   * list and reject the same stale bundles.
   *
   * Set ``policyUpdate: null`` to **disable** the install path
   * entirely (the receiver will warn-and-drop ``policy_updated``
   * events). Use that escape hatch only for debugging — production
   * deployments must keep the install path live.
   */
  policyUpdate?: PolicyUpdateOptions | null;
  /**
   * Shared circuit breaker — typically the same instance the
   * {@link import("./batcher.js").TelemetryBatcher} owns. When the
   * breaker is open, the receiver skips its SSE reconnect attempt
   * and sleeps for the breaker's jittered reset window instead of
   * burning a 90-second read timeout every cycle. Without a shared
   * breaker each subsystem retries independently — functional but
   * wasteful when the control plane is hard-down. Single source of
   * truth for control-plane health is the right pattern.
   */
  circuitBreaker?: CircuitBreaker | undefined;
}

/**
 * Default per-read SSE timeout. Exported so tests and callers can refer
 * to the same value the receiver uses.
 *
 * 120 seconds is the ceiling for any well-behaved deployment:
 *   - AWS ALB default idle timeout: 60s
 *   - Cloudflare free plan: 100s
 *   - Nginx default: 60s (keepalive_timeout)
 *
 * Set above all three so normal heartbeat intervals (every 30–60s)
 * cannot accidentally exceed this window and force a reconnect.
 */
export const DEFAULT_READ_TIMEOUT_MS = 120_000;

/** Diagnostic snapshot returned from {@link ControlReceiver.diagnostics}. */
export interface ReceiverDiagnostics {
  /** True if the receiver loop is running. */
  running: boolean;
  /** True if the most recent connection is open. */
  connected: boolean;
  /** Total reconnect attempts since start. */
  reconnects: number;
  /** Total events dispatched since start. */
  eventsReceived: number;
  /** Unix-ms of the most recent event or null. */
  lastEventAt: number | null;
}

/**
 * Long-lived control-plane subscription. One instance per agent per
 * process. Start once at init time, call {@link stop} at shutdown.
 */
export class ControlReceiver {
  private readonly controlPlaneUrl: string;
  private readonly apiKey: string;
  private readonly agentId: string;
  private readonly engine: ControlEngine;
  private readonly logger: Logger | undefined;
  private readonly fetchImpl: typeof fetch;
  private readonly initialBackoffMs: number;
  private readonly maxBackoffMs: number;
  private readonly pollIntervalMs: number;
  private readonly readTimeoutMs: number;
  private readonly apiVersion: string;
  private readonly policyUpdate: PolicyUpdateOptions | null;
  private readonly circuitBreaker: CircuitBreaker | undefined;

  private abort: AbortController | null = null;
  private runPromise: Promise<void> | null = null;
  private running = false;
  private connected = false;
  private reconnects = 0;
  private eventsReceived = 0;
  private lastEventAt: number | null = null;
  /**
   * Hash of the bundle currently installed by this receiver — `null`
   * until the first successful install. Mirrors the Python SDK's
   * `_last_installed_hash` field. Used by `handleControlEvent` (via
   * the per-call `policyUpdate.lastHash` getter wired below) to skip
   * the WASM `reload_policy_signed` call when an SSE/poll path
   * re-delivers the same active bundle — the OPA bundle / TUF
   * "don't re-apply unchanged" pattern. Without this, the WASM core's
   * strict-greater monotonic check would reject the idempotent
   * replay every time the receiver reconnects or polls.
   *
   * In-memory only on JS. The Python SDK additionally persists the
   * verified DSSE envelope to ``~/.checkrd/policy_state.json`` and
   * re-installs it on startup (OPA bundle / TUF client pattern), so
   * an SDK restart enforces policy from the first request — there's
   * no "engine empty until SSE init lands" window.
   *
   * That same persistence story doesn't translate cleanly here: Node
   * has ``fs``, Cloudflare Workers have KV, browsers have
   * localStorage / IndexedDB, Vercel Edge has nothing durable.
   * Following the OPA storage-plugin pattern, the right answer is a
   * pluggable ``PolicyStore`` interface (default: in-memory; Node
   * users plug in an ``fs``-backed adapter). Until that lands, JS
   * receivers boot with an empty engine on every restart and start
   * enforcing once SSE init delivers the active bundle (typically
   * sub-second on modern networks). The strict-greater monotonic
   * check still defends against in-process replay.
   */
  private lastInstalledHash: string | null = null;

  constructor(opts: ReceiverOptions) {
    this.controlPlaneUrl = opts.controlPlaneUrl.replace(/\/$/, "");
    this.apiKey = opts.apiKey;
    this.agentId = opts.agentId;
    this.engine = opts.engine;
    this.logger = opts.logger;
    this.fetchImpl = opts.fetch ?? globalThis.fetch.bind(globalThis);
    this.initialBackoffMs = opts.initialBackoffMs ?? 1_000;
    this.maxBackoffMs = opts.maxBackoffMs ?? 60_000;
    this.pollIntervalMs = opts.pollIntervalMs ?? 15_000;
    // `opts.readTimeoutMs === 0` is a valid explicit opt-out, NOT a
    // "use default" signal. Use nullish coalescing (?? ) so `0` stays
    // `0` and only `undefined`/`null` fall through to the default.
    this.readTimeoutMs = opts.readTimeoutMs ?? DEFAULT_READ_TIMEOUT_MS;
    this.apiVersion = opts.apiVersion ?? "";
    // Default the DSSE-install path ON: callers who want it disabled
    // must explicitly pass ``policyUpdate: null``. Mirrors the Python
    // SDK, where ``ControlReceiver`` always installs signed bundles
    // through ``_apply_policy_update`` — both wrappers must enforce
    // the same anti-rollback / freshness checks at runtime, otherwise
    // a JS-only deployment would silently lose the protection.
    if (opts.policyUpdate === null) {
      this.policyUpdate = null;
    } else if (opts.policyUpdate !== undefined) {
      this.policyUpdate = opts.policyUpdate;
    } else {
      this.policyUpdate = {
        loadTrustedKeys: () => trustedPolicyKeysJson(opts.logger),
      };
    }
    this.circuitBreaker = opts.circuitBreaker;
  }

  /**
   * Build the header set used for the GET subscribe and state-poll
   * requests. GET requests skip Idempotency-Key / Content-Type but
   * still carry the X-Checkrd-SDK-* family and optional Checkrd-Version
   * pin so operators can trace SSE connections through the same
   * dashboards that watch POST traffic.
   */
  private controlHeaders(accept: string): Record<string, string> {
    const headers: Record<string, string> = {
      "X-API-Key": this.apiKey,
      "User-Agent": `checkrd-js/${VERSION}`,
      Accept: accept,
      ...platformHeaders(),
    };
    if (this.apiVersion.length > 0) {
      headers["Checkrd-Version"] = this.apiVersion;
    }
    return headers;
  }

  /**
   * Begin the subscribe loop. Returns immediately; the reconnection
   * machinery runs in the background until {@link stop} is called.
   */
  start(): void {
    if (this.running) return;
    // Loud one-shot warning when production trust roots are missing AND
    // we're pointed at a production control plane — every signed policy
    // update would silently be rejected. Fired here (not at SDK import)
    // because controlPlaneUrl is only known at receiver-construct time
    // and only matters when we're about to start listening.
    warnIfMisconfigured({ baseUrl: this.controlPlaneUrl, logger: this.logger });
    this.running = true;
    this.abort = new AbortController();
    this.runPromise = this.loop().catch((err: unknown) => {
      this.logger?.error("control receiver crashed", { err });
    });
  }

  /**
   * Stop the loop and wait for the current connection to unwind.
   * Idempotent.
   */
  async stop(): Promise<void> {
    if (!this.running) return;
    this.running = false;
    this.abort?.abort();
    this.abort = null;
    const p = this.runPromise;
    this.runPromise = null;
    if (p) await p;
    this.connected = false;
  }

  /** Snapshot of receiver counters, for health probes. */
  diagnostics(): ReceiverDiagnostics {
    return {
      running: this.running,
      connected: this.connected,
      reconnects: this.reconnects,
      eventsReceived: this.eventsReceived,
      lastEventAt: this.lastEventAt,
    };
  }

  // -------------------------------------------------------------------
  // Internals
  // -------------------------------------------------------------------

  private async loop(): Promise<void> {
    let backoff = this.initialBackoffMs;
    // The while condition re-checks `running` on each iteration; `stop()`
    // flips it and aborts the controller, so long-running awaits unwind
    // into the `APIUserAbortError` path below, then the loop exits.
    while (this.isRunning()) {
      // Fast-fail when the shared circuit breaker says the control
      // plane is down. The batcher already discovered the outage —
      // there's no point burning a 90-second SSE read timeout to
      // re-confirm. Sleep through the breaker's reset window and try
      // again. When no breaker is wired in (the default), ``allow()``
      // returns true and the loop falls straight through to the
      // independent-backoff path.
      if (this.circuitBreaker !== undefined && !this.circuitBreaker.allow()) {
        await this.sleep(backoff);
        backoff = Math.min(this.maxBackoffMs, Math.round(backoff * 2));
        continue;
      }
      try {
        await this.pollStateOnce();
        // Successful poll — control plane is reachable even if SSE
        // is flapping. Tell the breaker so the batcher doesn't
        // fast-fail unnecessarily.
        this.circuitBreaker?.recordSuccess();
      } catch (err) {
        this.circuitBreaker?.recordFailure();
        this.logger?.debug("control poll failed (continuing to SSE)", { err });
      }
      try {
        await this.connect();
        // Clean disconnect (server-closed stream). Reset backoff
        // and signal control-plane health on the shared breaker.
        this.circuitBreaker?.recordSuccess();
        backoff = this.initialBackoffMs;
      } catch (err) {
        if (err instanceof APIUserAbortError) return;
        this.circuitBreaker?.recordFailure();
        this.logger?.warn("control SSE disconnected, backing off", {
          delay: backoff,
          err,
        });
        this.reconnects += 1;
      }
      await this.sleep(backoff);
      backoff = Math.min(this.maxBackoffMs, Math.round(backoff * 2));
    }
  }

  private isRunning(): boolean {
    return this.running;
  }

  private async connect(): Promise<void> {
    const url = `${this.controlPlaneUrl}/v1/agents/${encodeURIComponent(this.agentId)}/control`;
    const signal = this.abort?.signal;
    const init: RequestInit = {
      method: "GET",
      headers: this.controlHeaders("text/event-stream"),
    };
    if (signal) init.signal = signal;
    const response = await this.fetchImpl(url, init);
    if (!response.ok) {
      throw new Error(`control SSE HTTP ${response.status.toString()}`);
    }
    if (response.body === null) {
      throw new Error("control SSE response missing body");
    }
    this.connected = true;
    try {
      for await (const event of parseSSE(response.body, { signal, readTimeoutMs: this.readTimeoutMs })) {
        if (!this.isRunning()) return;
        this.eventsReceived += 1;
        this.lastEventAt = Date.now();
        const logger = this.logger
          ? { warn: this.logger.warn.bind(this.logger), error: this.logger.error.bind(this.logger) }
          : undefined;
        handleControlEvent(
          this.engine,
          event.name,
          event.data,
          logger,
          this.withHashCache(this.policyUpdate),
        );
      }
    } finally {
      this.connected = false;
    }
  }

  private async pollStateOnce(): Promise<void> {
    const url = `${this.controlPlaneUrl}/v1/agents/${encodeURIComponent(this.agentId)}/control/state`;
    const signal = this.abort?.signal;
    const init: RequestInit = {
      method: "GET",
      headers: this.controlHeaders("application/json"),
    };
    if (signal) init.signal = signal;
    const response = await this.fetchImpl(url, init);
    if (!response.ok) return;
    const text = await response.text();
    const parsed = JSON.parse(text) as {
      kill_switch_active?: unknown;
      active_policy_hash?: unknown;
      policy_envelope?: unknown;
    };
    const active = Boolean(parsed.kill_switch_active);
    this.engine.setKillSwitch(active);
    // Install signed policy from poll fallback when present. Same
    // `handleControlEvent` dispatcher as SSE so verification + hash-
    // cache idempotency run identically across paths. The synthesized
    // payload mirrors a `policy_updated` event so the install code
    // doesn't need a separate poll-shaped path.
    const envelope = parsed.policy_envelope;
    if (envelope !== undefined && envelope !== null) {
      const synthesized = JSON.stringify({
        policy_envelope: envelope,
        active_policy_hash: parsed.active_policy_hash,
      });
      const logger = this.logger
        ? {
            warn: this.logger.warn.bind(this.logger),
            error: this.logger.error.bind(this.logger),
          }
        : undefined;
      handleControlEvent(
        this.engine,
        "policy_updated",
        synthesized,
        logger,
        this.withHashCache(this.policyUpdate),
      );
    }
  }

  /**
   * Wrap the configured `policyUpdate` (or build a default one) with
   * `getLastHash` + `onInstalled` callbacks bound to this receiver's
   * in-memory hash cache. Called per-event so the closure always sees
   * the current value. Returns `undefined` if the install path is
   * disabled.
   */
  private withHashCache(
    base: PolicyUpdateOptions | null,
  ): PolicyUpdateOptions | undefined {
    if (base === null) return undefined;
    return {
      ...base,
      getLastHash: () => this.lastInstalledHash,
      onInstalled: async (version, hash) => {
        this.lastInstalledHash = hash;
        if (base.onInstalled) {
          await base.onInstalled(version, hash);
        }
      },
    };
  }

  private sleep(ms: number): Promise<void> {
    return new Promise((resolve, reject) => {
      const signal = this.abort?.signal;
      if (signal?.aborted) {
        reject(new APIUserAbortError());
        return;
      }
      const timer = setTimeout(() => {
        signal?.removeEventListener("abort", onAbort);
        resolve();
      }, ms);
      const onAbort = (): void => {
        clearTimeout(timer);
        reject(new APIUserAbortError());
      };
      signal?.addEventListener("abort", onAbort, { once: true });
      // Poll the state URL once at reconnect boundary. Suppressed if we
      // have no URL to hit.
      void this.pollWhileWaiting(ms);
    });
  }

  private async pollWhileWaiting(totalMs: number): Promise<void> {
    const deadline = Date.now() + totalMs;
    while (this.isRunning() && Date.now() < deadline) {
      const step = Math.min(this.pollIntervalMs, deadline - Date.now());
      if (step <= 0) return;
      await new Promise<void>((r) => setTimeout(r, step));
      try {
        await this.pollStateOnce();
      } catch (err) {
        this.logger?.debug("control poll failed", { err });
      }
    }
  }
}

// ---------------------------------------------------------------------------
// SSE stream parser
// ---------------------------------------------------------------------------

/** A single SSE event parsed from the wire. */
export interface SSEEvent {
  /** Event type (from `event: ...` line, or "message" if absent). */
  name: string;
  /** Concatenated data payload (joined by `\n` if multiple `data:` lines). */
  data: string;
}

/**
 * Hard upper bound on the buffered SSE payload between event boundaries.
 * Mirrors Python's ``_MAX_SSE_EVENT_BYTES``. A compromised or hostile
 * control plane could otherwise stream bytes without newline terminators
 * indefinitely and exhaust process memory; the cap converts a remote-
 * controllable OOM into a loud error that the reconnect loop catches.
 */
const MAX_SSE_EVENT_BYTES = 10 * 1024 * 1024;

/**
 * Parse an SSE wire stream into an async iterable of {@link SSEEvent}.
 *
 * Implements just enough of the SSE spec (whatwg.org/stream) to cover
 * the Checkrd control plane: `event:` + `data:` lines terminated by
 * blank line, ignoring `id:`, `retry:`, and comments.
 */
export async function* parseSSE(
  stream: ReadableStream<Uint8Array>,
  opts: { signal?: AbortSignal | undefined; readTimeoutMs?: number } = {},
): AsyncIterable<SSEEvent> {
  const reader = stream.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  let eventName = "message";
  let dataLines: string[] = [];

  const readStep = async (): Promise<ReadableStreamReadResult<Uint8Array>> => {
    if (opts.readTimeoutMs && opts.readTimeoutMs > 0) {
      return Promise.race([
        reader.read(),
        new Promise<ReadableStreamReadResult<Uint8Array>>((_, reject) => {
          setTimeout(() => { reject(new Error("sse read timeout")); }, opts.readTimeoutMs);
        }),
      ]);
    }
    return reader.read();
  };

  try {
    for (;;) {
      if (opts.signal?.aborted) throw new APIUserAbortError();
      const { value, done } = await readStep();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      if (buffer.length > MAX_SSE_EVENT_BYTES) {
        throw new Error(
          `SSE event exceeds ${MAX_SSE_EVENT_BYTES.toString()}-byte limit; aborting stream`,
        );
      }

      let boundary = buffer.indexOf("\n");
      while (boundary !== -1) {
        let line = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 1);
        if (line.endsWith("\r")) line = line.slice(0, -1);
        if (line.length === 0) {
          if (dataLines.length > 0) {
            yield { name: eventName, data: dataLines.join("\n") };
          }
          eventName = "message";
          dataLines = [];
        } else if (line.startsWith(":")) {
          // Comment line — ignored.
        } else if (line.startsWith("event:")) {
          eventName = line.slice(6).trimStart();
        } else if (line.startsWith("data:")) {
          dataLines.push(line.slice(5).trimStart());
        }
        boundary = buffer.indexOf("\n");
      }
    }
  } finally {
    try {
      reader.releaseLock();
    } catch {
      // releaseLock can throw if cancel was called; we don't care.
    }
  }
}
