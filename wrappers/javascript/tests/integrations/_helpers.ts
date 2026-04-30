/**
 * Shared helpers for vendor instrumentor tests. Each `test_<vendor>.ts`
 * file exercises the same lifecycle (instrument + uninstrument +
 * idempotency + missing-package safety) — keeping the boilerplate
 * here means a regression hits one helper, not seven copies.
 */
import { vi } from "vitest";

import { WasmEngine } from "../../src/engine.js";

const ALLOW_ALL = JSON.stringify({ agent: "t", default: "allow", rules: [] });

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
