/**
 * Tests for the process-wide stream-capture memory budget.
 *
 * Verifies:
 *   - acquire/release accounting under success and failure paths
 *   - the budget refuses requests that exceed remaining capacity
 *   - the dropped counter is monotonic and observable via diagnostics
 *   - `teeResponseForTokens` skips capture (passes the response through
 *     un-teed) when the budget is exhausted
 *   - on stream completion, the reservation is released so subsequent
 *     captures can proceed
 *
 * Together these are the guard against "N concurrent streams × 4 MB
 * per stream = N × 4 MB heap exposure" that the per-stream cap alone
 * cannot bound.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  StreamCaptureBudget,
  resetStreamCaptureBudgetForTests,
  setStreamCaptureBudgetCapacity,
  streamCaptureBudget,
  streamCaptureDiagnostics,
  teeResponseForTokens,
} from "../src/_stream_capture.js";

const FOUR_MB = 4 * 1024 * 1024;

function sseResponse(body: string): Response {
  return new Response(body, {
    status: 200,
    headers: { "content-type": "text/event-stream" },
  });
}

const noopSink = {
  enqueue: vi.fn(),
  close: vi.fn(),
};

const noopOpts = {
  vendor: "openai" as const,
  requestId: "req-test",
  url: "https://api.openai.com/v1/chat/completions",
  method: "POST",
  agentId: "agent-test",
  sink: noopSink,
  startMs: Date.now(),
};

describe("StreamCaptureBudget — accounting primitives", () => {
  it("acquires and releases capacity in lockstep", () => {
    const b = new StreamCaptureBudget(10_000);
    expect(b.acquire(4_000)).toBe(true);
    expect(b.diagnostics().inUseBytes).toBe(4_000);
    expect(b.acquire(5_000)).toBe(true);
    expect(b.diagnostics().inUseBytes).toBe(9_000);
    b.release(4_000);
    expect(b.diagnostics().inUseBytes).toBe(5_000);
  });

  it("refuses requests that would exceed capacity", () => {
    const b = new StreamCaptureBudget(10_000);
    expect(b.acquire(8_000)).toBe(true);
    // 8_000 + 4_000 > 10_000 → must fail and increment the counter
    expect(b.acquire(4_000)).toBe(false);
    expect(b.diagnostics().inUseBytes).toBe(8_000);
    expect(b.diagnostics().droppedBudget).toBe(1);
  });

  it("treats zero-byte and negative requests as free", () => {
    const b = new StreamCaptureBudget(10_000);
    expect(b.acquire(0)).toBe(true);
    expect(b.acquire(-1)).toBe(true);
    expect(b.diagnostics().inUseBytes).toBe(0);
    expect(b.diagnostics().droppedBudget).toBe(0);
  });

  it("clamps in-use at zero when over-released", () => {
    // Defensive: a buggy caller that releases more than it acquired
    // should not produce negative bookkeeping.
    const b = new StreamCaptureBudget(10_000);
    b.acquire(1_000);
    b.release(5_000);
    expect(b.diagnostics().inUseBytes).toBe(0);
  });

  it("rejects non-finite or negative capacity at construction", () => {
    expect(() => new StreamCaptureBudget(Number.NaN)).toThrow();
    expect(() => new StreamCaptureBudget(-1)).toThrow();
    expect(() => new StreamCaptureBudget(Infinity)).toThrow();
  });

  it("setCapacity validates the same way the constructor does", () => {
    const b = new StreamCaptureBudget(10_000);
    expect(() => {
      b.setCapacity(-1);
    }).toThrow();
    expect(() => {
      b.setCapacity(Number.NaN);
    }).toThrow();
    expect(() => {
      b.setCapacity(20_000);
    }).not.toThrow();
    expect(b.diagnostics().capacityBytes).toBe(20_000);
  });

  it("counts dropped acquires monotonically", () => {
    const b = new StreamCaptureBudget(1_000);
    b.acquire(900);
    expect(b.acquire(200)).toBe(false);
    expect(b.acquire(200)).toBe(false);
    expect(b.acquire(200)).toBe(false);
    expect(b.diagnostics().droppedBudget).toBe(3);
  });
});

describe("teeResponseForTokens — budget gate", () => {
  beforeEach(() => {
    resetStreamCaptureBudgetForTests();
    noopSink.enqueue.mockClear();
  });

  it("captures when budget has room", async () => {
    setStreamCaptureBudgetCapacity(FOUR_MB);
    const body = `data: {"usage":{"prompt_tokens":10,"completion_tokens":5}}\n\ndata: [DONE]\n\n`;
    const response = sseResponse(body);
    const teed = teeResponseForTokens(response, noopOpts);
    // Drain the consumer side so the capture-side completes.
    await teed.text();
    // Wait one microtask so the fire-and-forget capture loop releases.
    await new Promise<void>((r) => {
      setTimeout(r, 5);
    });
    expect(streamCaptureDiagnostics().inUseBytes).toBe(0);
    expect(streamCaptureDiagnostics().droppedBudget).toBe(0);
    expect(noopSink.enqueue).toHaveBeenCalledTimes(1);
  });

  it("skips capture (returns original response) when budget is exhausted", async () => {
    // Pre-reserve nearly the entire budget so the next 4MB acquire fails.
    setStreamCaptureBudgetCapacity(FOUR_MB);
    expect(streamCaptureBudget.acquire(FOUR_MB)).toBe(true);

    const body = `data: {"usage":{"prompt_tokens":1,"completion_tokens":1}}\n\ndata: [DONE]\n\n`;
    const response = sseResponse(body);

    const out = teeResponseForTokens(response, noopOpts);
    // Same Response object means we did NOT tee — the budget gate
    // returned the original. `tee()` always produces fresh ReadableStreams,
    // so identity check is the cleanest assertion.
    expect(out).toBe(response);
    expect(streamCaptureDiagnostics().droppedBudget).toBeGreaterThanOrEqual(1);
    expect(noopSink.enqueue).not.toHaveBeenCalled();

    // Cleanup the manual reservation.
    streamCaptureBudget.release(FOUR_MB);
  });

  it("releases the reservation after the capture completes", async () => {
    setStreamCaptureBudgetCapacity(FOUR_MB * 2);
    const body = `data: {"usage":{"prompt_tokens":3,"completion_tokens":3}}\n\ndata: [DONE]\n\n`;

    const t1 = teeResponseForTokens(sseResponse(body), noopOpts);
    // Mid-flight: one stream's worth reserved.
    expect(streamCaptureDiagnostics().inUseBytes).toBe(FOUR_MB);
    await t1.text();
    await new Promise<void>((r) => {
      setTimeout(r, 5);
    });
    // After the capture loop unwinds, the reservation is back.
    expect(streamCaptureDiagnostics().inUseBytes).toBe(0);

    // A second stream should cleanly reserve again.
    const t2 = teeResponseForTokens(sseResponse(body), noopOpts);
    expect(streamCaptureDiagnostics().inUseBytes).toBe(FOUR_MB);
    await t2.text();
    await new Promise<void>((r) => {
      setTimeout(r, 5);
    });
    expect(streamCaptureDiagnostics().inUseBytes).toBe(0);
  });

  it("releases the reservation even if the capture loop throws", async () => {
    // Force the capture path to fail by feeding a sink whose enqueue
    // throws synchronously. The capture loop must still release the
    // budget so the next stream can proceed.
    setStreamCaptureBudgetCapacity(FOUR_MB);
    const angrySink = {
      enqueue: vi.fn().mockImplementation(() => {
        throw new Error("boom");
      }),
      close: vi.fn(),
    };
    const body = `data: {"usage":{"prompt_tokens":1}}\n\ndata: [DONE]\n\n`;

    const out = teeResponseForTokens(sseResponse(body), {
      ...noopOpts,
      sink: angrySink,
    });
    await out.text();
    await new Promise<void>((r) => {
      setTimeout(r, 5);
    });

    expect(streamCaptureDiagnostics().inUseBytes).toBe(0);
  });
});

afterEach(() => {
  resetStreamCaptureBudgetForTests();
});
