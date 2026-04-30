/**
 * Test-only helpers. Mirrors
 * ``wrappers/python/src/checkrd/testing.py``.
 *
 * Import from the ``checkrd/testing`` subpath to avoid pulling the
 * WASM engine into tests that don't need it:
 *
 *     import { MockEngine, mockWrap } from "checkrd/testing";
 */

import { wrapFetch, type FetchFn, type WrapFetchOptions } from "./transports/fetch.js";
import type {
  EvaluateRequest,
  EvalResult,
  Keypair,
  SignedBatch,
  WasmEngine,
} from "./engine.js";

/** Callback form: `(method, url, headers, body) => boolean` allows on true. */
export type MockPolicyFn = (
  method: string,
  url: string,
  headers: [string, string][],
  body: string | null,
) => boolean;

/** Options for {@link MockEngine}. */
export interface MockEngineOptions {
  /**
   * Default decision when no `policyFn` is supplied, or the `policyFn`
   * returns `undefined`. `"allow"` (default) mimics observation mode;
   * `"deny"` is useful for testing fail-closed code paths.
   */
  default?: "allow" | "deny";
  /** Override the decision with a caller-provided predicate. */
  policyFn?: MockPolicyFn;
  /** Optional deny reason when the decision is deny. */
  denyReason?: string;
}

/**
 * A drop-in replacement for {@link WasmEngine} that skips the WASM
 * binary entirely. Tests that only need to exercise transport-level
 * behavior (hooks, header redaction, streaming, error propagation)
 * can use this to avoid paying the WASM instantiation cost and
 * avoid needing the built `checkrd_core.wasm` file on disk.
 *
 * The structural subset implemented here is exactly what
 * `wrapFetch` and the instrumentors touch at runtime. Code that
 * reaches for the full `WasmEngine` API (e.g. `signTelemetryBatch`)
 * will correctly surface a thrown error.
 */
export class MockEngine {
  /** Events the engine would have emitted; populated for test inspection. */
  readonly events: EvaluateRequest[] = [];

  private readonly defaultAllow: boolean;
  private readonly denyReason: string;
  private readonly policyFn: MockPolicyFn | undefined;
  private _killSwitch = false;

  constructor(opts: MockEngineOptions = {}) {
    this.defaultAllow = (opts.default ?? "allow") === "allow";
    this.denyReason = opts.denyReason ?? "policy denied (mock)";
    this.policyFn = opts.policyFn;
  }

  evaluate(req: EvaluateRequest): EvalResult {
    this.events.push(req);
    if (this._killSwitch) {
      return {
        allowed: false,
        deny_reason: "kill switch active",
        telemetry_json: "",
        request_id: req.request_id,
      };
    }
    const decision = this.policyFn
      ? this.policyFn(req.method, req.url, req.headers, req.body)
      : this.defaultAllow;
    if (decision) {
      return {
        allowed: true,
        telemetry_json: "",
        request_id: req.request_id,
      };
    }
    return {
      allowed: false,
      deny_reason: this.denyReason,
      telemetry_json: "",
      request_id: req.request_id,
    };
  }

  setKillSwitch(active: boolean): void {
    this._killSwitch = active;
  }

  reloadPolicy(_policyJson: string): void {
    // no-op; caller is testing policy-reload plumbing, not WASM state
    void _policyJson;
  }

  reloadPolicySigned(_opts: unknown): void {
    throw new Error("MockEngine.reloadPolicySigned: not implemented in tests");
  }

  getActivePolicyVersion(): number {
    return 0;
  }

  setInitialPolicyVersion(_version: number): void {
    void _version;
  }

  sign(_payload: Uint8Array): Uint8Array | null {
    return null;
  }

  signTelemetryBatch(_opts: unknown): SignedBatch | null {
    return null;
  }

  static generateKeypair(): Keypair {
    // Deterministic all-zero keypair — tests MUST NOT rely on
    // signature verification of these values.
    return {
      privateKey: new Uint8Array(32),
      publicKey: new Uint8Array(32),
    };
  }

  static derivePublicKey(_priv: Uint8Array): Uint8Array {
    return new Uint8Array(32);
  }
}

/**
 * Wrap a base `fetch` with a {@link MockEngine}. Convenience around
 * {@link wrapFetch} so tests don't construct the mock separately.
 */
export function mockWrap(
  baseFetch: FetchFn,
  opts: MockEngineOptions & Omit<WrapFetchOptions, "engine"> = { enforce: true, agentId: "test" },
): FetchFn {
  const engineOpts: MockEngineOptions = {};
  if (opts.default !== undefined) engineOpts.default = opts.default;
  if (opts.policyFn !== undefined) engineOpts.policyFn = opts.policyFn;
  if (opts.denyReason !== undefined) engineOpts.denyReason = opts.denyReason;
  const engine = new MockEngine(engineOpts);
  const { default: _d, policyFn: _p, denyReason: _r, ...rest } = opts;
  void _d;
  void _p;
  void _r;
  return wrapFetch(baseFetch, {
    ...rest,
    engine: engine as unknown as WasmEngine,
    enforce: rest.enforce,
    agentId: rest.agentId,
  });
}
