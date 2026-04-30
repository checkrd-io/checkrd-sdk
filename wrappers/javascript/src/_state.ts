/**
 * Global SDK state mirroring `checkrd/_state.py`. JS is single-threaded
 * per isolate, so unlike the Python wrapper we do not need a lock —
 * there is no concurrent access to worry about within one event loop.
 *
 * This module exists for `init()` / `instrument()` users who configure
 * the SDK once at startup and then let instrumented SDKs flow through it
 * without plumbing options on every call.
 */

import type { WasmEngine } from "./engine.js";
import type { BeforeRequestHook, OnAllowHook, OnDenyHook } from "./hooks.js";
import type { Settings } from "./_settings.js";
import type { TelemetrySink } from "./sinks.js";
import type { TelemetryBatcher } from "./batcher.js";
import type { ControlReceiver } from "./receiver.js";
import type { Logger } from "./_logger.js";

/** Internal: the live runtime, present only after a successful init(). */
export interface GlobalContext {
  /** WASM engine instance. */
  engine: WasmEngine;
  /** Whether denied requests should be raised as errors. */
  enforce: boolean;
  /** Resolved settings snapshot. */
  settings: Settings;
  /** Optional user callbacks. */
  onAllow: OnAllowHook | undefined;
  onDeny: OnDenyHook | undefined;
  beforeRequest: BeforeRequestHook | undefined;
  /** Tracks whether the engine is in a degraded (pass-through) state. */
  degraded: boolean;
  /** Unix ms timestamp of the last evaluate() call, for health probes. */
  lastEvalAt: number | null;
  /** Active telemetry sink, if control plane is configured. */
  sink: TelemetrySink | undefined;
  /** Underlying batcher, when the sink is the control-plane sink. */
  batcher: TelemetryBatcher | undefined;
  /** Optional SSE receiver. */
  receiver: ControlReceiver | undefined;
  /** Active logger. */
  logger: Logger;
}

let _context: GlobalContext | null = null;
let _degraded = false;

/** Get the current context or throw if init() has not been called. */
export function getContext(): GlobalContext {
  if (!_context) {
    throw new Error(
      "checkrd: init() has not been called. Call checkrd.init({...}) before instrument().",
    );
  }
  return _context;
}

/** Return the context if present; otherwise return null. */
export function maybeContext(): GlobalContext | null {
  return _context;
}

/** Whether any context exists (degraded or healthy). */
export function hasContext(): boolean {
  return _context !== null;
}

/** Install a fresh context. Any existing context is replaced. */
export function setContext(ctx: GlobalContext | null): void {
  _context = ctx;
}

/** Mark the SDK as degraded (engine unavailable, pass-through mode). */
export function setDegraded(value: boolean): void {
  _degraded = value;
}

/** True if the SDK is in degraded state. */
export function isDegraded(): boolean {
  return _degraded;
}

/** Record a successful evaluate() timestamp for health reporting. */
export function recordEvalAt(ts: number): void {
  if (_context) _context.lastEvalAt = ts;
}

/** Return the last evaluate() timestamp, or null if no evaluations have occurred. */
export function getLastEvalAt(): number | null {
  return _context?.lastEvalAt ?? null;
}
