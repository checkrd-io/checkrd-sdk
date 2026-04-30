/**
 * Tests for the browser-unload flush path:
 *   - {@link attachBrowserUnloadFlush}: listener wiring + detach
 *   - {@link TelemetryBatcher.urgentFlush}: fire-and-forget keepalive
 *     POST, body trimming when over the 64 KiB Fetch keepalive cap,
 *     fail-closed on signing errors.
 *
 * Browser-unload contract this file pins (jsdom-level):
 *   - ``keepalive: true`` is set on every unload-path POST so the
 *     browser keeps the request alive past navigation per the Fetch
 *     spec (``§5.4 The fetch() method``).
 *   - Signature / Content-Digest / Signer-Agent / Instance-Id /
 *     DSSE-Envelope headers ride along — the unload path must NEVER
 *     ship unsigned telemetry, and the signature is computed over the
 *     same bytes that get sent (verified by ``maybeSign`` capture
 *     below).
 *   - Body integrity: events round-trip from ``enqueue`` →
 *     ``pagehide`` → ``fetch`` body unchanged.
 *   - Body size respects the {@link URGENT_FLUSH_BODY_LIMIT_BYTES} cap
 *     even after trimming, so the browser never silently drops the
 *     whole request for being over the 64 KiB Fetch keepalive ceiling.
 *
 * What we DO NOT cover here:
 *   - Real browser unload race conditions (network shutdown timing,
 *     Safari's stricter keepalive policy, Chrome's per-process 64 KiB
 *     budget shared with other in-flight keepalive POSTs). Those
 *     require a real browser. Playwright + a static page that calls
 *     ``window.location.href = ...`` while the SDK is mid-flight is
 *     the right next layer; tracked as a future addition once the
 *     dashboard E2E suite picks up Playwright (currently only the
 *     dashboard's Storybook tests use it).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { attachBrowserUnloadFlush } from "../src/_browser_flush.js";
import {
  TelemetryBatcher,
  URGENT_FLUSH_BODY_LIMIT_BYTES,
  type BatcherOptions,
} from "../src/batcher.js";
import type { WasmEngine } from "../src/engine.js";

// ---------------------------------------------------------------------------
// Test doubles
// ---------------------------------------------------------------------------

/** Minimal mock engine that signs every batch with deterministic stub data. */
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

function makeBatcher(
  overrides: Partial<BatcherOptions> & {
    fetch?: typeof fetch;
  } = {},
): TelemetryBatcher {
  const fetchImpl = overrides.fetch ??
    (vi.fn().mockResolvedValue(
      new Response(null, { status: 204 }),
    ) as unknown as typeof fetch);
  return new TelemetryBatcher({
    controlPlaneUrl: "https://api.example.com",
    apiKey: "ck_test_xx",
    agentId: "agent-test",
    engine: fakeEngine(),
    fetch: fetchImpl,
    ...overrides,
  });
}

// ---------------------------------------------------------------------------
// urgentFlush — body composition
// ---------------------------------------------------------------------------

describe("TelemetryBatcher.urgentFlush", () => {
  it("is a no-op when the queue is empty", () => {
    const fetchImpl = vi.fn();
    const batcher = makeBatcher({ fetch: fetchImpl as unknown as typeof fetch });
    batcher.urgentFlush();
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("fires fetch with keepalive: true and signature headers", async () => {
    const fetchImpl = vi
      .fn()
      .mockResolvedValue(new Response(null, { status: 204 }));
    const batcher = makeBatcher({ fetch: fetchImpl as unknown as typeof fetch });
    batcher.enqueue({ event_type: "test", request_id: "r-1" });
    batcher.urgentFlush();

    expect(fetchImpl).toHaveBeenCalledTimes(1);
    const call = fetchImpl.mock.calls[0]!;
    const url = call[0] as string;
    const init = call[1] as RequestInit;
    expect(url).toBe("https://api.example.com/v1/telemetry");
    const requestInit = init as RequestInit;
    expect(requestInit.method).toBe("POST");
    expect(requestInit.keepalive).toBe(true);
    const headers = requestInit.headers as Record<string, string>;
    expect(headers["Signature"]).toBeDefined();
    expect(headers["Content-Digest"]).toBeDefined();
    expect(headers["X-Checkrd-Signer-Agent"]).toBe("agent-test");
    expect(headers["traceparent"]).toMatch(/^00-[0-9a-f]{32}-[0-9a-f]{16}-01$/);

    // Allow the awaiting promise to settle so `sent` is incremented.
    await new Promise<void>((r) => {
      setTimeout(r, 5);
    });
    expect(batcher.diagnostics().sent).toBe(1);
  });

  it("does not retry — single attempt only", async () => {
    const fetchImpl = vi
      .fn()
      .mockRejectedValue(new Error("network is down"));
    const batcher = makeBatcher({ fetch: fetchImpl as unknown as typeof fetch });
    batcher.enqueue({ event_type: "test", request_id: "r-1" });
    batcher.urgentFlush();

    await new Promise<void>((r) => {
      setTimeout(r, 10);
    });
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    expect(batcher.diagnostics().droppedSendError).toBe(1);
  });

  it("drops the batch (does not send unsigned) when signing fails", () => {
    const fetchImpl = vi.fn();
    // Engine that returns null → `maybeSign` throws.
    const angryEngine = {
      signTelemetryBatch: vi.fn().mockReturnValue(null),
    } as unknown as WasmEngine;
    const batcher = makeBatcher({
      engine: angryEngine,
      fetch: fetchImpl as unknown as typeof fetch,
    });
    batcher.enqueue({ event_type: "test", request_id: "r-1" });
    batcher.urgentFlush();
    expect(fetchImpl).not.toHaveBeenCalled();
    expect(batcher.diagnostics().droppedSendError).toBe(1);
  });

  it("trims OLDEST events first when batch exceeds the keepalive budget", () => {
    const fetchImpl = vi
      .fn()
      .mockResolvedValue(new Response(null, { status: 204 }));
    // High batchSize so `enqueue` does not auto-flush — we want
    // the queue intact when `urgentFlush()` runs so the trim
    // logic actually exercises.
    const batcher = makeBatcher({
      fetch: fetchImpl as unknown as typeof fetch,
      batchSize: 100_000,
    });
    // Each event is ~1 KiB of request_id. With 100 events we land
    // comfortably above the 60 KiB budget; the batcher must drop the
    // oldest until the body fits.
    const eventCount = 100;
    const padding = "x".repeat(1024);
    for (let i = 0; i < eventCount; i++) {
      batcher.enqueue({ event_type: "test", request_id: `r-${i.toString()}-${padding}` });
    }
    batcher.urgentFlush();

    expect(fetchImpl).toHaveBeenCalledTimes(1);
    const call = fetchImpl.mock.calls[0]!;
    const init = call[1] as RequestInit;
    const body = init.body as string;
    expect(new TextEncoder().encode(body).byteLength).toBeLessThanOrEqual(
      URGENT_FLUSH_BODY_LIMIT_BYTES,
    );
    // Some events were dropped — counter must reflect it.
    expect(batcher.diagnostics().droppedSendError).toBeGreaterThan(0);
  });
});

// ---------------------------------------------------------------------------
// attachBrowserUnloadFlush — listener wiring
// ---------------------------------------------------------------------------

describe("attachBrowserUnloadFlush", () => {
  let target: EventTarget;
  let urgentFlushSpy: ReturnType<typeof vi.spyOn>;
  let batcher: TelemetryBatcher;

  beforeEach(() => {
    target = new EventTarget();
    batcher = makeBatcher();
    urgentFlushSpy = vi.spyOn(batcher, "urgentFlush").mockImplementation(() => {
      // Don't actually send anything in these tests.
    });
  });

  afterEach(() => {
    urgentFlushSpy.mockRestore();
  });

  it("returns a no-op detach when no window-like global is available", () => {
    // Pass `null` to simulate edge runtimes (Cloudflare Workers,
    // Vercel Edge, Deno script mode) where there is no window.
    const detach = attachBrowserUnloadFlush(batcher, {
      target: undefined as unknown as EventTarget,
    });
    detach();
    expect(urgentFlushSpy).not.toHaveBeenCalled();
  });

  it("calls urgentFlush on pagehide", () => {
    attachBrowserUnloadFlush(batcher, { target });
    target.dispatchEvent(new Event("pagehide"));
    expect(urgentFlushSpy).toHaveBeenCalledTimes(1);
  });

  it("calls urgentFlush on beforeunload", () => {
    attachBrowserUnloadFlush(batcher, { target });
    target.dispatchEvent(new Event("beforeunload"));
    expect(urgentFlushSpy).toHaveBeenCalledTimes(1);
  });

  it("registers both listeners — fires twice if both events arrive", () => {
    // Some browsers fire both for a single navigation. The batcher
    // tolerates the duplicate (queue is empty after the first call).
    attachBrowserUnloadFlush(batcher, { target });
    target.dispatchEvent(new Event("pagehide"));
    target.dispatchEvent(new Event("beforeunload"));
    expect(urgentFlushSpy).toHaveBeenCalledTimes(2);
  });

  it("detach removes both listeners", () => {
    const detach = attachBrowserUnloadFlush(batcher, { target });
    detach();
    target.dispatchEvent(new Event("pagehide"));
    target.dispatchEvent(new Event("beforeunload"));
    expect(urgentFlushSpy).not.toHaveBeenCalled();
  });

  it("detach is idempotent", () => {
    const detach = attachBrowserUnloadFlush(batcher, { target });
    detach();
    detach();
    detach();
    target.dispatchEvent(new Event("pagehide"));
    expect(urgentFlushSpy).not.toHaveBeenCalled();
  });

  it("does not surface throws from urgentFlush onto the unload path", () => {
    urgentFlushSpy.mockImplementation(() => {
      throw new Error("boom");
    });
    const logger = {
      debug: vi.fn(),
      info: vi.fn(),
      warn: vi.fn(),
      error: vi.fn(),
    };
    attachBrowserUnloadFlush(batcher, { target, logger });
    expect(() => target.dispatchEvent(new Event("pagehide"))).not.toThrow();
    expect(logger.warn).toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// End-to-end: pagehide → real urgentFlush → keepalive POST
// ---------------------------------------------------------------------------
//
// The blocks above stub ``urgentFlush`` to isolate listener wiring.
// This block does the opposite: lets the real ``urgentFlush`` run so
// the contract that ``pagehide`` ships authenticated, integrity-
// preserving, keepalive-tagged requests is locked end-to-end.

describe("pagehide → urgentFlush → fetch (integrated)", () => {
  it("pagehide sends a single keepalive POST with full signature envelope", async () => {
    const target = new EventTarget();
    const fetchImpl = vi
      .fn()
      .mockResolvedValue(new Response(null, { status: 204 }));
    const batcher = makeBatcher({ fetch: fetchImpl as unknown as typeof fetch });

    batcher.enqueue({ event_type: "test", request_id: "r-A" });
    batcher.enqueue({ event_type: "test", request_id: "r-B" });

    attachBrowserUnloadFlush(batcher, { target });
    target.dispatchEvent(new Event("pagehide"));

    expect(fetchImpl).toHaveBeenCalledTimes(1);
    const [, init] = fetchImpl.mock.calls[0]! as [string, RequestInit];

    // 1. ``keepalive: true`` is the load-bearing bit. Without it the
    //    browser cancels the request when the document tears down.
    expect(init.keepalive).toBe(true);

    // 2. The full signature envelope must ride along — never an
    //    unsigned ship from the unload path.
    const headers = init.headers as Record<string, string>;
    expect(headers["Signature"]).toBeDefined();
    expect(headers["Signature-Input"]).toBeDefined();
    expect(headers["Content-Digest"]).toBeDefined();
    expect(headers["X-Checkrd-Signer-Agent"]).toBe("agent-test");
    expect(headers["X-Checkrd-DSSE-Envelope"]).toBeDefined();
    expect(headers["X-Checkrd-Instance-Id"]).toBeDefined();
    expect(headers["traceparent"]).toMatch(
      /^00-[0-9a-f]{32}-[0-9a-f]{16}-01$/,
    );

    // 3. Body integrity: every enqueued event arrives intact, in
    //    order, with no duplicate or dropped entries.
    const body = init.body as string;
    const parsed = JSON.parse(body) as Array<{ request_id: string }>;
    expect(parsed.map((e) => e.request_id)).toEqual(["r-A", "r-B"]);

    // 4. Body size never exceeds the keepalive ceiling.
    expect(new TextEncoder().encode(body).byteLength).toBeLessThanOrEqual(
      URGENT_FLUSH_BODY_LIMIT_BYTES,
    );

    // Wait for the awaited promise so ``sent`` increments before we
    // observe diagnostics.
    await new Promise<void>((r) => {
      setTimeout(r, 5);
    });
    expect(batcher.diagnostics().sent).toBe(2);
  });

  it("pagehide and beforeunload produce identical body bytes for the same queue", () => {
    // Same batcher, fired twice via two separate listener attachments.
    // The second event sees an empty queue, so this isn't testing
    // "same body twice" — it's testing that the routing converges on
    // the same code path. The first event's captured body must match
    // what we'd see if we re-enqueued and fired ``beforeunload``
    // instead.
    const sequenceA = (() => {
      const t = new EventTarget();
      const f = vi
        .fn()
        .mockResolvedValue(new Response(null, { status: 204 }));
      const b = makeBatcher({ fetch: f as unknown as typeof fetch });
      b.enqueue({ event_type: "test", request_id: "x-1" });
      b.enqueue({ event_type: "test", request_id: "x-2" });
      attachBrowserUnloadFlush(b, { target: t });
      t.dispatchEvent(new Event("pagehide"));
      return f.mock.calls[0]![1] as RequestInit;
    })();
    const sequenceB = (() => {
      const t = new EventTarget();
      const f = vi
        .fn()
        .mockResolvedValue(new Response(null, { status: 204 }));
      const b = makeBatcher({ fetch: f as unknown as typeof fetch });
      b.enqueue({ event_type: "test", request_id: "x-1" });
      b.enqueue({ event_type: "test", request_id: "x-2" });
      attachBrowserUnloadFlush(b, { target: t });
      t.dispatchEvent(new Event("beforeunload"));
      return f.mock.calls[0]![1] as RequestInit;
    })();

    // Body bytes are deterministic given the same enqueue order.
    // Headers vary (timestamps, instance ids, signature
    // randomization), but the JSON.stringified payload must not.
    expect(sequenceA.body).toBe(sequenceB.body);
    expect(sequenceA.keepalive).toBe(true);
    expect(sequenceB.keepalive).toBe(true);
  });

  it("body trimming under the keepalive cap still keeps the queue tail intact", () => {
    // When we go over the 60 KiB budget, FIFO trim drops the OLDEST
    // events. The body that ships must be a prefix of the original
    // queue from some index to the end — never an interior slice or
    // a re-ordered batch.
    const fetchImpl = vi
      .fn()
      .mockResolvedValue(new Response(null, { status: 204 }));
    const batcher = makeBatcher({
      fetch: fetchImpl as unknown as typeof fetch,
      batchSize: 100_000, // disable auto-flush
    });
    const padding = "x".repeat(1024);
    const eventCount = 100;
    for (let i = 0; i < eventCount; i++) {
      batcher.enqueue({
        event_type: "test",
        request_id: `r-${i.toString().padStart(3, "0")}-${padding}`,
      });
    }
    batcher.urgentFlush();

    expect(fetchImpl).toHaveBeenCalledTimes(1);
    const init = fetchImpl.mock.calls[0]![1] as RequestInit;
    const body = init.body as string;
    expect(new TextEncoder().encode(body).byteLength).toBeLessThanOrEqual(
      URGENT_FLUSH_BODY_LIMIT_BYTES,
    );

    // Parsed body is a contiguous tail of the original sequence —
    // each event's request_id is one greater than the last, and the
    // last entry is r-099 (newest never trimmed).
    const parsed = JSON.parse(body) as Array<{ request_id: string }>;
    expect(parsed.length).toBeGreaterThan(0);
    const ids = parsed.map((e) => Number.parseInt(e.request_id.slice(2, 5), 10));
    for (let i = 1; i < ids.length; i++) {
      expect(ids[i]).toBe((ids[i - 1] ?? 0) + 1);
    }
    expect(ids[ids.length - 1]).toBe(eventCount - 1);

    // Drops counter reflects the trimmed prefix.
    expect(batcher.diagnostics().droppedSendError).toBe(eventCount - parsed.length);
  });

  it("signature header binds the bytes that actually ship, not the pre-trim batch", () => {
    // Capture the bytes ``signTelemetryBatch`` sees and the bytes
    // ``fetch`` sees. They MUST be equal — otherwise an attacker who
    // intercepts the request could substitute an over-budget body
    // and the server's verifier would still pass the signature.
    let signedBytes: Uint8Array | null = null;
    const engine = {
      signTelemetryBatch: vi
        .fn()
        .mockImplementation((args: { batchJson: Uint8Array }) => {
          // Copy by-value so a later mutation by the batcher couldn't
          // alias what we captured.
          signedBytes = new Uint8Array(args.batchJson);
          return {
            content_digest: "sha-256=:abc=:",
            signature_input: 'sig=("@target-uri")',
            signature: "sig=:zzz=:",
            dsse_envelope: "{}",
            instance_id: "0123456789abcdef",
            expires: Math.floor(Date.now() / 1000) + 300,
          };
        }),
    } as unknown as WasmEngine;
    const fetchImpl = vi
      .fn()
      .mockResolvedValue(new Response(null, { status: 204 }));
    const batcher = makeBatcher({
      fetch: fetchImpl as unknown as typeof fetch,
      engine,
      batchSize: 100_000,
    });
    const padding = "x".repeat(1024);
    for (let i = 0; i < 100; i++) {
      batcher.enqueue({ event_type: "test", request_id: `r-${i.toString()}-${padding}` });
    }
    batcher.urgentFlush();

    expect(fetchImpl).toHaveBeenCalledTimes(1);
    const init = fetchImpl.mock.calls[0]![1] as RequestInit;
    const sentBytes = new TextEncoder().encode(init.body as string);

    expect(signedBytes).not.toBeNull();
    // Same byte length AND same contents — signature was computed
    // over the actual ship.
    expect(signedBytes!.byteLength).toBe(sentBytes.byteLength);
    expect(Array.from(signedBytes!)).toEqual(Array.from(sentBytes));
  });
});
