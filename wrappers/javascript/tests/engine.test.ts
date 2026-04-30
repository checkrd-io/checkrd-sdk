import { describe, expect, it } from "vitest";

import { WasmEngine } from "../src/engine.js";
import { CheckrdInitError } from "../src/exceptions.js";

const TS = "2026-03-28T14:30:00Z";
const TS_MS = 1_774_708_200_000;

const ALLOW_ALL = JSON.stringify({
  agent: "test-agent",
  default: "allow",
  rules: [],
});

const DENY_ALL = JSON.stringify({
  agent: "test-agent",
  default: "deny",
  rules: [],
});

describe("WasmEngine construction", () => {
  it("loads and initializes with an allow-all policy", () => {
    expect(() => new WasmEngine(ALLOW_ALL, "test-agent")).not.toThrow();
  });

  it("rejects malformed policy JSON with CheckrdInitError", () => {
    expect(() => new WasmEngine("{not-json", "test-agent")).toThrow(CheckrdInitError);
  });
});

describe("WasmEngine.evaluate", () => {
  it("allows every request under an allow-all policy", () => {
    const engine = new WasmEngine(ALLOW_ALL, "test-agent");
    const res = engine.evaluate({
      request_id: "req-1",
      method: "GET",
      url: "https://api.openai.com/v1/chat/completions",
      headers: [["Content-Type", "application/json"]],
      body: null,
      timestamp: TS,
      timestamp_ms: TS_MS,
    });
    expect(res.allowed).toBe(true);
    expect(res.deny_reason).toBeUndefined();
    expect(res.request_id).toBe("req-1");
  });

  it("denies every request under a deny-all policy", () => {
    const engine = new WasmEngine(DENY_ALL, "test-agent");
    const res = engine.evaluate({
      request_id: "req-2",
      method: "POST",
      url: "https://api.example.com/",
      headers: [],
      body: null,
      timestamp: TS,
      timestamp_ms: TS_MS,
    });
    expect(res.allowed).toBe(false);
    expect(res.deny_reason).toBeTruthy();
  });

  it("echoes request_id unchanged (FFI round-trip sanity)", () => {
    const engine = new WasmEngine(ALLOW_ALL, "test-agent");
    const id = "req-with-emoji-🦀-and-multi-byte-中";
    const res = engine.evaluate({
      request_id: id,
      method: "GET",
      url: "https://example.com/",
      headers: [],
      body: null,
      timestamp: TS,
      timestamp_ms: TS_MS,
    });
    expect(res.request_id).toBe(id);
  });
});

describe("WasmEngine.setKillSwitch", () => {
  it("activates kill switch and denies subsequent requests", () => {
    const engine = new WasmEngine(ALLOW_ALL, "test-agent");
    engine.setKillSwitch(true);
    const res = engine.evaluate({
      request_id: "req-k",
      method: "GET",
      url: "https://example.com/",
      headers: [],
      body: null,
      timestamp: TS,
      timestamp_ms: TS_MS,
    });
    expect(res.allowed).toBe(false);
    engine.setKillSwitch(false);
    const res2 = engine.evaluate({
      request_id: "req-k2",
      method: "GET",
      url: "https://example.com/",
      headers: [],
      body: null,
      timestamp: TS,
      timestamp_ms: TS_MS,
    });
    expect(res2.allowed).toBe(true);
  });
});

describe("WasmEngine.reloadPolicy", () => {
  it("swaps the active policy without re-init", () => {
    const engine = new WasmEngine(ALLOW_ALL, "test-agent");
    expect(
      engine.evaluate({
        request_id: "r", method: "GET", url: "https://x/",
        headers: [], body: null, timestamp: TS, timestamp_ms: TS_MS,
      }).allowed,
    ).toBe(true);
    engine.reloadPolicy(DENY_ALL);
    expect(
      engine.evaluate({
        request_id: "r", method: "GET", url: "https://x/",
        headers: [], body: null, timestamp: TS, timestamp_ms: TS_MS,
      }).allowed,
    ).toBe(false);
  });
});

describe("WasmEngine.getActivePolicyVersion", () => {
  it("returns 0 before any signed bundle is installed", () => {
    const engine = new WasmEngine(ALLOW_ALL, "test-agent");
    expect(engine.getActivePolicyVersion()).toBe(0);
  });
});

describe("WasmEngine.sign (anonymous mode)", () => {
  it("returns null when no private key is configured", () => {
    const engine = new WasmEngine(ALLOW_ALL, "test-agent");
    const sig = engine.sign(new Uint8Array([1, 2, 3]));
    expect(sig).toBeNull();
  });
});

describe("WasmEngine.signTelemetryBatch (anonymous mode)", () => {
  it("returns null when no private key is configured", () => {
    const engine = new WasmEngine(ALLOW_ALL, "test-agent");
    const result = engine.signTelemetryBatch({
      batchJson: new TextEncoder().encode('{"events":[]}'),
      targetUri: "https://api.checkrd.io/v1/telemetry",
      signerAgent: "test-agent",
      nonce: "abc123",
      created: 1_700_000_000,
      expires: 1_700_000_060,
    });
    expect(result).toBeNull();
  });
});

describe("WasmEngine static crypto helpers", () => {
  it("generateKeypair returns (32, 32) Ed25519 bytes", () => {
    const { privateKey, publicKey } = WasmEngine.generateKeypair();
    expect(privateKey).toBeInstanceOf(Uint8Array);
    expect(publicKey).toBeInstanceOf(Uint8Array);
    expect(privateKey.length).toBe(32);
    expect(publicKey.length).toBe(32);
  });

  it("derivePublicKey reproduces the stored public half", () => {
    const { privateKey, publicKey } = WasmEngine.generateKeypair();
    const derived = WasmEngine.derivePublicKey(privateKey);
    expect(derived).toEqual(publicKey);
  });

  it("derivePublicKey rejects non-32-byte keys", () => {
    expect(() => WasmEngine.derivePublicKey(new Uint8Array(16))).toThrow(
      CheckrdInitError,
    );
  });
});
