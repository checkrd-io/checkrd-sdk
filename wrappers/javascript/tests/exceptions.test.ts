import { describe, expect, it } from "vitest";

import {
  CheckrdInitError,
  CheckrdPolicyDenied,
  FFI_ERROR_REASONS,
  PolicySignatureError,
} from "../src/exceptions.js";

describe("CheckrdInitError", () => {
  it("derives an invalid_policy code from policy-related messages", () => {
    const err = new CheckrdInitError("invalid policy JSON");
    expect(err.code).toBe("invalid_policy");
    expect(err).toBeInstanceOf(Error);
    expect(err.name).toBe("CheckrdInitError");
  });

  it("derives wasm_integrity_failed on integrity messages", () => {
    const err = new CheckrdInitError("WASM integrity check failed");
    expect(err.code).toBe("wasm_integrity_failed");
  });

  it("falls back to the generic code", () => {
    const err = new CheckrdInitError("something else broke");
    expect(err.code).toBe("checkrd_error");
  });

  it("preserves the original error via ES2022 cause chain", () => {
    const original = new SyntaxError("Unexpected token } at position 12");
    const err = new CheckrdInitError("WASM returned malformed JSON", {
      cause: original,
    });
    expect((err as { cause?: unknown }).cause).toBe(original);
    // Verify the chain renders in the stack — Node folds `cause` into
    // the trace, which is the operator-visible diagnosis path.
    expect(String(err.stack)).toContain("WASM returned malformed JSON");
  });

  it("works without options (backward-compatible signature)", () => {
    const err = new CheckrdInitError("just a message");
    expect((err as { cause?: unknown }).cause).toBeUndefined();
  });
});

describe("CheckrdPolicyDenied", () => {
  it("carries all deny details and derives a code", () => {
    const err = new CheckrdPolicyDenied({
      reason: "rate limit exceeded",
      requestId: "req-1",
      url: "https://api.openai.com/v1/chat/completions",
      dashboardUrl: "https://dash",
    });
    expect(err.message).toBe("rate limit exceeded");
    expect(err.code).toBe("rate_limit_exceeded");
    expect(err.requestId).toBe("req-1");
    expect(err.url).toContain("openai.com");
    expect(err.dashboardUrl).toBe("https://dash");
  });
});

describe("PolicySignatureError", () => {
  it("maps FFI codes to stable reason labels", () => {
    const err = new PolicySignatureError(-11);
    expect(err.ffiCode).toBe(-11);
    // Mirrors Python's `_FFI_ERROR_REASONS[-11]`. Single source of truth
    // across both SDKs so dashboards group on identical labels.
    expect(err.code).toBe("bundle_version_not_monotonic");
    expect(err.code).toBe(FFI_ERROR_REASONS[-11]);
    expect(err.reason).toBe("bundle_version_not_monotonic");
  });

  it("handles unknown FFI codes without crashing", () => {
    const err = new PolicySignatureError(-999);
    expect(err.code).toMatch(/unknown_ffi_code_/);
  });
});
