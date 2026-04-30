/**
 * Tests for the ``beforeSend`` telemetry-event mutation hook.
 *
 * Sentry-pattern hook: the SDK invokes ``beforeSend(event, hint)`` once
 * per ``enqueue`` call right before the event lands in the batcher's
 * queue. Returning the (possibly mutated) event ships it; returning
 * ``null`` drops it; throwing logs and drops.
 *
 * The hook is the only mutation surface on the telemetry pipeline.
 * Read-only hooks (``OnAllowHook`` / ``OnDenyHook``) stay; this adds
 * the operator-controlled drop-or-rewrite path.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  TelemetryBatcher,
  type BeforeSendHint,
  type TelemetryEvent,
} from "../src/batcher.js";
import type { WasmEngine } from "../src/engine.js";

function fakeEngine(): WasmEngine {
  return {
    signTelemetryBatch: vi.fn().mockReturnValue({
      content_digest: "sha-256=:abc=:",
      signature_input: 'sig=("@target-uri")',
      signature: "sig=:zzz=:",
      dsse_envelope: "{}",
      instance_id: "0123456789abcdef",
      expires: Math.floor(Date.now() / 1000) + 300,
    }),
  } as unknown as WasmEngine;
}

const noopFetch = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));

describe("TelemetryBatcher.beforeSend", () => {
  let batcher: TelemetryBatcher | undefined;

  beforeEach(() => {
    noopFetch.mockClear();
  });

  afterEach(async () => {
    if (batcher !== undefined) {
      await batcher.stop();
      batcher = undefined;
    }
  });

  it("invokes the hook with (event, hint) on every enqueue", () => {
    const beforeSend = vi.fn(
      (event: TelemetryEvent, _hint: BeforeSendHint) => event,
    );
    batcher = new TelemetryBatcher({
      controlPlaneUrl: "https://api.example.com",
      apiKey: "ck_test",
      agentId: "agent-test",
      engine: fakeEngine(),
      fetch: noopFetch as unknown as typeof fetch,
      beforeSend,
    });
    batcher.enqueue({ event_type: "request_evaluation", url: "x" });
    expect(beforeSend).toHaveBeenCalledTimes(1);
    const [event, hint] = beforeSend.mock.calls[0]!;
    expect(event).toMatchObject({ event_type: "request_evaluation", url: "x" });
    expect(hint.agent_id).toBe("agent-test");
    expect(hint.event_kind).toBe("request_evaluation");
  });

  it("ships the mutated event the hook returns (Sentry pattern)", () => {
    // Redact the URL — common pattern for operators who don't want
    // raw URLs in their telemetry pipeline.
    const beforeSend: typeof TelemetryBatcher.prototype["enqueue"] extends never
      ? never
      : (event: TelemetryEvent) => TelemetryEvent | null =
      (event) => ({ ...event, url: "[redacted]" });
    batcher = new TelemetryBatcher({
      controlPlaneUrl: "https://api.example.com",
      apiKey: "ck_test",
      agentId: "agent-test",
      engine: fakeEngine(),
      fetch: noopFetch as unknown as typeof fetch,
      beforeSend,
    });
    batcher.enqueue({ event_type: "test", url: "https://secret.example/x" });
    // Mutated event is in the queue; ``pending`` shows count.
    expect(batcher.diagnostics().pending).toBe(1);
  });

  it("drops the event when the hook returns null", () => {
    const beforeSend = vi.fn().mockReturnValue(null);
    batcher = new TelemetryBatcher({
      controlPlaneUrl: "https://api.example.com",
      apiKey: "ck_test",
      agentId: "agent-test",
      engine: fakeEngine(),
      fetch: noopFetch as unknown as typeof fetch,
      beforeSend,
    });
    batcher.enqueue({ event_type: "test" });
    // Operator-intended drop → no counters move, queue stays empty.
    expect(batcher.diagnostics().pending).toBe(0);
    expect(batcher.diagnostics().droppedBackpressure).toBe(0);
    expect(batcher.diagnostics().droppedSendError).toBe(0);
  });

  it("drops the event (and logs) when the hook throws", () => {
    const logger = {
      debug: vi.fn(),
      info: vi.fn(),
      warn: vi.fn(),
      error: vi.fn(),
    };
    const beforeSend = vi.fn().mockImplementation(() => {
      throw new Error("hook crashed");
    });
    batcher = new TelemetryBatcher({
      controlPlaneUrl: "https://api.example.com",
      apiKey: "ck_test",
      agentId: "agent-test",
      engine: fakeEngine(),
      fetch: noopFetch as unknown as typeof fetch,
      beforeSend,
      logger,
    });
    expect(() => { batcher!.enqueue({ event_type: "test" }); }).not.toThrow();
    expect(batcher.diagnostics().pending).toBe(0);
    expect(logger.error).toHaveBeenCalled();
  });

  it("falls through unchanged when no hook is configured (default behaviour)", () => {
    batcher = new TelemetryBatcher({
      controlPlaneUrl: "https://api.example.com",
      apiKey: "ck_test",
      agentId: "agent-test",
      engine: fakeEngine(),
      fetch: noopFetch as unknown as typeof fetch,
    });
    batcher.enqueue({ event_type: "request_evaluation" });
    expect(batcher.diagnostics().pending).toBe(1);
  });

  it("populates ``hint.event_kind`` from the event's ``event_type`` field", () => {
    const captured: BeforeSendHint[] = [];
    batcher = new TelemetryBatcher({
      controlPlaneUrl: "https://api.example.com",
      apiKey: "ck_test",
      agentId: "agent-test",
      engine: fakeEngine(),
      fetch: noopFetch as unknown as typeof fetch,
      beforeSend: (event, hint) => {
        captured.push(hint);
        return event;
      },
    });
    batcher.enqueue({ event_type: "stream_completion", url: "x" });
    batcher.enqueue({ event_type: "request_evaluation", url: "y" });
    batcher.enqueue({ url: "z" }); // no event_type → default
    expect(captured.map((h) => h.event_kind)).toEqual([
      "stream_completion",
      "request_evaluation",
      "request_evaluation",
    ]);
  });
});
