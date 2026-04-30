import { describe, expect, it } from "vitest";

import { CircuitBreaker } from "../src/_circuit_breaker.js";

describe("CircuitBreaker", () => {
  it("starts closed and allows traffic", () => {
    const cb = new CircuitBreaker();
    expect(cb.allow()).toBe(true);
    expect(cb.diagnostics().state).toBe("closed");
  });

  it("opens after consecutive failures", () => {
    const cb = new CircuitBreaker({ failureThreshold: 3 });
    cb.recordFailure();
    expect(cb.allow()).toBe(true);
    cb.recordFailure();
    expect(cb.allow()).toBe(true);
    cb.recordFailure();
    expect(cb.allow()).toBe(false);
    expect(cb.diagnostics().state).toBe("open");
  });

  it("resets on success", () => {
    const cb = new CircuitBreaker({ failureThreshold: 2 });
    cb.recordFailure();
    cb.recordSuccess();
    cb.recordFailure();
    expect(cb.allow()).toBe(true);
  });

  it("transitions open -> half_open after resetAfterMs", () => {
    let now = 1_000;
    const cb = new CircuitBreaker({
      failureThreshold: 1,
      resetAfterMs: 500,
      // Deterministic transition test: disable jitter so the boundary
      // is exactly at resetAfterMs. Jitter behavior is validated in the
      // dedicated tests below.
      resetJitterMs: 0,
      now: (): number => now,
    });
    cb.recordFailure();
    expect(cb.allow()).toBe(false);
    now += 600;
    expect(cb.allow()).toBe(true);
    expect(cb.diagnostics().state).toBe("half_open");
  });

  it("half_open failure reopens immediately", () => {
    let now = 1_000;
    const cb = new CircuitBreaker({
      failureThreshold: 1,
      resetAfterMs: 100,
      resetJitterMs: 0,
      now: (): number => now,
    });
    cb.recordFailure();
    now += 200;
    expect(cb.allow()).toBe(true); // half_open
    cb.recordFailure();
    expect(cb.allow()).toBe(false); // open again
  });

  it("half_open success fully closes the circuit", () => {
    let now = 1_000;
    const cb = new CircuitBreaker({
      failureThreshold: 1,
      resetAfterMs: 100,
      resetJitterMs: 0,
      now: (): number => now,
    });
    cb.recordFailure();
    now += 200;
    cb.allow();
    cb.recordSuccess();
    expect(cb.diagnostics().state).toBe("closed");
    expect(cb.diagnostics().consecutiveFailures).toBe(0);
  });

  describe("reset jitter", () => {
    it("adds random jitter on top of resetAfterMs when the breaker opens", () => {
      // Deterministic random returning 0.5 → jitter is exactly 50 ms
      // for a 100 ms jitter window. Effective reset window = 200 + 50.
      const cb = new CircuitBreaker({
        failureThreshold: 1,
        resetAfterMs: 200,
        resetJitterMs: 100,
        random: (): number => 0.5,
      });
      cb.recordFailure();
      const diag = cb.diagnostics();
      expect(diag.state).toBe("open");
      expect(diag.effectiveResetMs).toBe(250);
    });

    it("re-rolls jitter on every open-event so consecutive opens differ", () => {
      const samples = [0.0, 0.999];
      let i = 0;
      let now = 0;
      const cb = new CircuitBreaker({
        failureThreshold: 1,
        resetAfterMs: 1_000,
        resetJitterMs: 1_000,
        now: (): number => now,
        random: (): number => {
          const v = samples[i % samples.length]!;
          i += 1;
          return v;
        },
      });
      // First open — jitter sample 0.0 → effective 1000 ms.
      cb.recordFailure();
      const first = cb.diagnostics().effectiveResetMs;
      expect(first).toBe(1000);
      // Wait long enough for the half_open transition, then fail again
      // and observe the second jitter sample (0.999 → effective 1999).
      now += 1_500;
      expect(cb.allow()).toBe(true);
      cb.recordFailure();
      const second = cb.diagnostics().effectiveResetMs;
      expect(second).toBe(1_999);
    });

    it("clamps jitter sample to non-negative resetJitterMs", () => {
      // Defensive: reject a misconfiguration that would produce
      // negative reset windows. Constructor must throw — silently
      // applying the bad value would let breakers reset instantly,
      // which is worse than failing loudly at startup.
      expect(() => new CircuitBreaker({ resetJitterMs: -1 })).toThrow();
    });

    it("zero jitter produces deterministic reset window", () => {
      const cb = new CircuitBreaker({
        failureThreshold: 1,
        resetAfterMs: 500,
        resetJitterMs: 0,
        // With jitter == 0 the random source is never sampled. Failing
        // hard if it WERE sampled is the cheapest way to assert that.
        random: (): number => {
          throw new Error("random must not be sampled when jitter is 0");
        },
      });
      cb.recordFailure();
      expect(cb.diagnostics().effectiveResetMs).toBe(500);
    });
  });
});
