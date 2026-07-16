/**
 * Shared helpers for vendor instrumentor tests. Each `test_<vendor>.ts`
 * file exercises the same lifecycle (instrument + uninstrument +
 * idempotency + missing-package safety) — keeping the boilerplate
 * here means a regression hits one helper, not seven copies.
 */
import { vi } from "vitest";

import { WasmEngine } from "../../src/engine.js";
import type { TelemetrySink, TelemetryEvent } from "../../src/sinks.js";

const ALLOW_ALL = JSON.stringify({ agent: "t", mode: "enforce", default: "allow", rules: [] });

/** Construct a fresh `InstrumentorOptions` for a single test case. */
export function makeInstrumentorOptions(): {
  engine: WasmEngine;
  enforce: boolean;
  agentId: string;
  baseFetch: typeof fetch;
} {
  return {
    engine: new WasmEngine(ALLOW_ALL, "test-agent"),
    enforce: true,
    agentId: "test-agent",
    baseFetch: vi.fn(async () => new Response("ok", { status: 200 })) as unknown as typeof fetch,
  };
}

/** Handles a capturing test uses to assert on what the wrapped fetch did. */
export interface CapturingInstrumentorHandles {
  options: {
    engine: WasmEngine;
    enforce: boolean;
    agentId: string;
    baseFetch: typeof fetch;
    sink: TelemetrySink;
  };
  /** Every telemetry event the wrapped fetch enqueued. */
  events: TelemetryEvent[];
  /** Every URL the recording base fetch was asked to fetch. */
  fetchCalls: string[];
}

/**
 * Instrumentor options wired to a capturing sink + a recording base
 * fetch. Lets a vendor test assert BOTH that the wrapped fetch was
 * injected into the vendor client AND that driving that fetch flows a
 * request through to the base fetch and emits a telemetry event — the
 * two halves of "real instrumentation works" the lifecycle-only tests
 * never exercised.
 */
export function makeCapturingInstrumentorOptions(): CapturingInstrumentorHandles {
  const events: TelemetryEvent[] = [];
  const fetchCalls: string[] = [];
  const sink: TelemetrySink = {
    enqueue(event: TelemetryEvent): void {
      events.push(event);
    },
    close(): Promise<void> {
      return Promise.resolve();
    },
  };
  const baseFetch = (async (input: RequestInfo | URL): Promise<Response> => {
    fetchCalls.push(input instanceof Request ? input.url : String(input));
    return new Response("{}", {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  }) as unknown as typeof fetch;
  return {
    options: {
      engine: new WasmEngine(ALLOW_ALL, "test-agent"),
      enforce: true,
      agentId: "test-agent",
      baseFetch,
      sink,
    },
    events,
    fetchCalls,
  };
}
