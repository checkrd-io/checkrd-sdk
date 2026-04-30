import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";

import { WasmEngine, __resetWasmModuleCache } from "../src/engine.js";
import { initAsync, wrapAsync, shutdown } from "../src/index.js";
import { CheckrdInitError } from "../src/exceptions.js";

const ALLOW_ALL = JSON.stringify({ agent: "t", default: "allow", rules: [] });
const DENY_ALL = JSON.stringify({ agent: "t", default: "deny", rules: [] });

// Path to the bundled binary relative to this test file.
const wasmPath = fileURLToPath(
  new URL("../checkrd_core.wasm", import.meta.url),
);

afterEach(async () => {
  await shutdown();
  __resetWasmModuleCache();
});

describe("WasmEngine.create — async factory", () => {
  it("constructs an engine from a caller-supplied Uint8Array", async () => {
    const bytes = await readFile(wasmPath);
    const engine = await WasmEngine.create(ALLOW_ALL, "test-agent", {
      wasm: new Uint8Array(bytes),
    });
    const result = engine.evaluate({
      request_id: "r",
      method: "GET",
      url: "https://example.com/",
      headers: [],
      body: null,
      timestamp: "",
      timestamp_ms: 0,
    });
    expect(result.allowed).toBe(true);
  });

  it("accepts an ArrayBuffer source", async () => {
    const bytes = await readFile(wasmPath);
    const ab = bytes.buffer.slice(
      bytes.byteOffset,
      bytes.byteOffset + bytes.byteLength,
    );
    const engine = await WasmEngine.create(ALLOW_ALL, "test-agent", {
      wasm: ab,
    });
    expect(
      engine.evaluate({
        request_id: "r",
        method: "GET",
        url: "https://example.com/",
        headers: [],
        body: null,
        timestamp: "",
        timestamp_ms: 0,
      }).allowed,
    ).toBe(true);
  });

  it("accepts a Response source", async () => {
    const bytes = await readFile(wasmPath);
    const response = new Response(new Uint8Array(bytes), {
      status: 200,
      headers: { "content-type": "application/wasm" },
    });
    const engine = await WasmEngine.create(ALLOW_ALL, "test-agent", {
      wasm: response,
    });
    expect(
      engine.evaluate({
        request_id: "r",
        method: "GET",
        url: "u",
        headers: [],
        body: null,
        timestamp: "",
        timestamp_ms: 0,
      }).allowed,
    ).toBe(true);
  });

  it("accepts a pre-compiled WebAssembly.Module (skips integrity)", async () => {
    const bytes = await readFile(wasmPath);
    const mod = await WebAssembly.compile(new Uint8Array(bytes));
    const engine = await WasmEngine.create(ALLOW_ALL, "test-agent", {
      wasm: mod,
    });
    expect(
      engine.evaluate({
        request_id: "r",
        method: "GET",
        url: "u",
        headers: [],
        body: null,
        timestamp: "",
        timestamp_ms: 0,
      }).allowed,
    ).toBe(true);
  });

  it("enforces the deny-by-default policy", async () => {
    const bytes = await readFile(wasmPath);
    const engine = await WasmEngine.create(DENY_ALL, "test-agent", {
      wasm: new Uint8Array(bytes),
    });
    const result = engine.evaluate({
      request_id: "r",
      method: "POST",
      url: "https://api.openai.com/v1/chat/completions",
      headers: [],
      body: null,
      timestamp: "",
      timestamp_ms: 0,
    });
    expect(result.allowed).toBe(false);
    expect(result.deny_reason).toBeTruthy();
  });

  it("prewarm() with explicit source does not touch the default cache", async () => {
    const bytes = await readFile(wasmPath);
    // Explicit-source prewarm should succeed without raising.
    await WasmEngine.prewarm(new Uint8Array(bytes));
    // Subsequent explicit-source create should also succeed.
    const engine = await WasmEngine.create(ALLOW_ALL, "prewarm-agent", {
      wasm: new Uint8Array(bytes),
    });
    expect(engine).toBeTruthy();
  });

  it("rejects with a directional error when fetch fails", async () => {
    // Explicit https:// URL forces the fetch path. Without it, on
    // Node ESM the default URL resolves to ``file://...`` which the
    // SDK now handles via ``fs.readFile`` — which would succeed and
    // bypass this regression test.
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(async () => {
        throw new TypeError("fetch failed");
      });
    try {
      await expect(
        WasmEngine.create(ALLOW_ALL, "test-agent", {
          wasm: new URL("https://example.invalid/checkrd_core.wasm"),
        }),
      ).rejects.toBeInstanceOf(CheckrdInitError);
    } finally {
      fetchSpy.mockRestore();
    }
  });
});

describe("initAsync / wrapAsync", () => {
  it("initAsync wires up the engine from WASM bytes", async () => {
    const bytes = await readFile(wasmPath);
    await initAsync({
      policy: ALLOW_ALL,
      agentId: "async-agent",
      wasm: new Uint8Array(bytes),
    });
    // Exercise the global context: the instrumentors should build.
    const { instrument, uninstrument } = await import("../src/index.js");
    expect(() => {
      instrument();
      uninstrument();
    }).not.toThrow();
  });

  it("wrapAsync returns a fetch-shaped function that enforces policy", async () => {
    const bytes = await readFile(wasmPath);
    const base = vi.fn(async () => new Response("{}", { status: 200 }));
    const f = await wrapAsync(base as unknown as typeof fetch, {
      policy: ALLOW_ALL,
      agentId: "async-agent",
      wasm: new Uint8Array(bytes),
    });
    const res = await f("https://example.com/");
    expect(res.status).toBe(200);
    expect(base).toHaveBeenCalledOnce();
  });

  it("wrapAsync blocks denied requests under deny-all", async () => {
    const bytes = await readFile(wasmPath);
    const base = vi.fn(async () => new Response("{}", { status: 200 }));
    const f = await wrapAsync(base as unknown as typeof fetch, {
      policy: DENY_ALL,
      agentId: "async-agent",
      enforce: true,
      wasm: new Uint8Array(bytes),
    });
    await expect(f("https://example.com/")).rejects.toMatchObject({
      name: "CheckrdPolicyDenied",
    });
    expect(base).not.toHaveBeenCalled();
  });

  it("initAsync respects CHECKRD_DISABLED", async () => {
    process.env["CHECKRD_DISABLED"] = "1";
    try {
      const bytes = await readFile(wasmPath);
      await initAsync({
        policy: ALLOW_ALL,
        agentId: "t",
        wasm: new Uint8Array(bytes),
      });
      const { healthy } = await import("../src/index.js");
      expect(healthy().status).toBe("disabled");
    } finally {
      delete process.env["CHECKRD_DISABLED"];
    }
  });
});
