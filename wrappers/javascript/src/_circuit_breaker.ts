/**
 * Minimal circuit breaker for control-plane calls. Three states:
 *
 *   - **closed**: requests flow normally; consecutive failures count.
 *   - **open**: every request fails fast without hitting the network
 *     until `resetAfterMs` elapses.
 *   - **half-open**: one probe request is allowed; success closes the
 *     circuit, failure re-opens it.
 *
 * Scoped per-instance so the batcher and the public-key registrar can
 * share one instance (or use separate ones), and so tests can construct
 * a breaker with a tight timeout.
 */

/** State transitions observable from diagnostics / logs. */
export type CircuitState = "closed" | "open" | "half_open";

/** Options for {@link CircuitBreaker}. */
export interface CircuitBreakerOptions {
  /** Consecutive failures before the circuit opens. Default: 5. */
  failureThreshold?: number;
  /** Base time the circuit stays open before admitting a probe. Default: 30_000 ms. */
  resetAfterMs?: number;
  /**
   * Maximum random jitter added on top of {@link resetAfterMs} when the
   * circuit opens. Each open-event picks a fresh value in
   * ``[0, resetJitterMs]`` so independent breakers tripped by a shared
   * outage do not all probe at the same instant — a thundering herd
   * that would re-overwhelm a recovering control plane and trip every
   * breaker again immediately. Default: 5_000 ms (~17 % of the base).
   *
   * Pattern: AWS Builders Library "exponential backoff and jitter",
   * applied to the reset window instead of the retry delay.
   * Set to ``0`` to opt out (not recommended in production).
   */
  resetJitterMs?: number;
  /** Override of the clock; test-only. */
  now?: () => number;
  /**
   * Override of the random source. Test-only — production callers
   * use ``Math.random``. Pass a function returning a value in
   * ``[0, 1)`` to produce deterministic jitter for assertions.
   */
  random?: () => number;
}

/** Diagnostic snapshot. */
export interface CircuitBreakerDiagnostics {
  state: CircuitState;
  consecutiveFailures: number;
  openedAt: number | null;
  /**
   * Time (ms) the breaker will remain open from {@link openedAt}
   * before admitting a half-open probe. Equals ``resetAfterMs`` plus
   * the jitter rolled at the most recent open-event. ``null`` when
   * the breaker has never opened in this process.
   */
  effectiveResetMs: number | null;
}

/**
 * Small, synchronous circuit breaker. Instances are not thread-safe
 * in the abstract, but JS's single-threaded event loop makes them
 * trivially safe per-isolate. Callers wire `allow()` before each
 * attempt and `recordSuccess()` / `recordFailure()` after.
 */
export class CircuitBreaker {
  private readonly failureThreshold: number;
  private readonly resetAfterMs: number;
  private readonly resetJitterMs: number;
  private readonly now: () => number;
  private readonly random: () => number;

  private state: CircuitState = "closed";
  private consecutiveFailures = 0;
  private openedAt: number | null = null;
  // Effective reset window for the current open-event = resetAfterMs +
  // a fresh random sample in [0, resetJitterMs]. Re-rolled every time
  // the circuit transitions into ``open`` so two consecutive opens of
  // the same breaker have different recovery windows. Null while the
  // circuit is closed (no jitter has been rolled yet).
  private currentResetMs: number | null = null;

  constructor(opts: CircuitBreakerOptions = {}) {
    this.failureThreshold = opts.failureThreshold ?? 5;
    this.resetAfterMs = opts.resetAfterMs ?? 30_000;
    this.resetJitterMs = opts.resetJitterMs ?? 5_000;
    if (this.resetJitterMs < 0) {
      throw new Error(
        `CircuitBreaker.resetJitterMs must be >= 0; got ${this.resetJitterMs.toString()}`,
      );
    }
    this.now = opts.now ?? ((): number => Date.now());
    this.random = opts.random ?? ((): number => Math.random());
  }

  /**
   * Called before each outbound attempt. Returns `true` if the attempt
   * should proceed, `false` if the circuit is open. Transitions from
   * `open` to `half_open` when the (jittered) reset window has elapsed.
   */
  allow(): boolean {
    if (this.state === "closed") return true;
    if (this.state === "half_open") return true;
    const now = this.now();
    const reset = this.currentResetMs ?? this.resetAfterMs;
    if (this.openedAt !== null && now - this.openedAt >= reset) {
      this.state = "half_open";
      return true;
    }
    return false;
  }

  /** Record a successful attempt. Resets the failure counter. */
  recordSuccess(): void {
    this.consecutiveFailures = 0;
    this.state = "closed";
    this.openedAt = null;
    this.currentResetMs = null;
  }

  /** Record a failed attempt. May open the circuit. */
  recordFailure(): void {
    this.consecutiveFailures += 1;
    if (
      this.state === "half_open" ||
      this.consecutiveFailures >= this.failureThreshold
    ) {
      this.state = "open";
      this.openedAt = this.now();
      // Roll fresh jitter on every open-event so two breakers tripped
      // by the same outage do not probe at the same instant. Math.random
      // returns in [0, 1) so the addition lies in [0, resetJitterMs).
      // Short-circuit when jitter is 0 — the random source is never
      // called, which keeps the explicit-opt-out path observable in
      // tests that mock ``random`` to throw.
      const jitter =
        this.resetJitterMs === 0
          ? 0
          : Math.floor(this.random() * this.resetJitterMs);
      this.currentResetMs = this.resetAfterMs + jitter;
    }
  }

  /** Current state for diagnostics. */
  diagnostics(): CircuitBreakerDiagnostics {
    return {
      state: this.state,
      consecutiveFailures: this.consecutiveFailures,
      openedAt: this.openedAt,
      effectiveResetMs: this.currentResetMs,
    };
  }

  /** Test-only reset. */
  reset(): void {
    this.state = "closed";
    this.consecutiveFailures = 0;
    this.openedAt = null;
    this.currentResetMs = null;
  }
}
