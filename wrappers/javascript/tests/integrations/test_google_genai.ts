/**
 * Google GenAI instrumentor tests.
 *
 * `@google/genai` is an OPTIONAL peer dep, not in devDependencies. Two
 * contracts are verified:
 *
 *   1. **Missing-package no-op** — silent no-op when the package is
 *      absent.
 *   2. **Real instrumentation** — the construct-trap fetch-injection
 *      runs. The Google SDK is the odd one out: it nests the fetch
 *      override under `httpOptions.fetch` rather than a top-level
 *      `fetch`, and the instrumentor must merge it in NON-destructively
 *      (preserving a caller's other `httpOptions`). We mock
 *      `lazyRequireOptional` to hand the instrumentor a minimal module
 *      matching `@google/genai`'s `GoogleGenAI` shape; the real
 *      `patchModuleExport` still runs.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { GoogleGenAIInstrumentor } from "../../src/integrations/_google_genai.js";

import { makeCapturingInstrumentorOptions, makeInstrumentorOptions } from "./_helpers.js";

const mockState = vi.hoisted(() => ({ module: undefined as unknown }));

vi.mock("../../src/integrations/_require.js", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../src/integrations/_require.js")>();
  return {
    ...actual, // keep the REAL patchModuleExport
    lazyRequireOptional: () => (name: string): unknown => {
      if (name === "@google/genai" && mockState.module !== undefined) return mockState.module;
      throw new Error(`@google/genai not installed (mock): ${name}`);
    },
  };
});

class FakeGoogleGenAI {
  public readonly config: Record<string, unknown>;
  constructor(config: Record<string, unknown> = {}) {
    this.config = config;
  }
}

/** Fresh fake `@google/genai` module exposing the `GoogleGenAI` client. */
function fakeGoogleModule(): { GoogleGenAI: unknown } {
  return { GoogleGenAI: FakeGoogleGenAI };
}

/** Read the nested `httpOptions` bag off a constructed fake client. */
function httpOptionsOf(client: { config: Record<string, unknown> }): Record<string, unknown> {
  return client.config.httpOptions as Record<string, unknown>;
}

afterEach(() => {
  mockState.module = undefined;
});

describe("GoogleGenAIInstrumentor (@google/genai not installed)", () => {
  it("instrument() is a silent no-op", () => {
    const instr = new GoogleGenAIInstrumentor(makeInstrumentorOptions());
    expect(() => { instr.instrument(); }).not.toThrow();
    instr.uninstrument();
  });

  it("uninstrument() is a silent no-op", () => {
    const instr = new GoogleGenAIInstrumentor(makeInstrumentorOptions());
    expect(() => { instr.uninstrument(); }).not.toThrow();
  });

  it("is idempotent under repeated calls", () => {
    const instr = new GoogleGenAIInstrumentor(makeInstrumentorOptions());
    instr.instrument();
    instr.instrument();
    expect(instr.isInstalled).toBe(true);
    instr.uninstrument();
    instr.uninstrument();
    expect(instr.isInstalled).toBe(false);
  });
});

describe("GoogleGenAIInstrumentor (real instrumentation)", () => {
  it("injects the wrapped fetch under httpOptions.fetch", () => {
    const mod = fakeGoogleModule();
    mockState.module = mod;
    const { options } = makeCapturingInstrumentorOptions();
    const instr = new GoogleGenAIInstrumentor(options);
    instr.instrument();
    try {
      const Patched = mod.GoogleGenAI as new (o: Record<string, unknown>) => { config: Record<string, unknown> };
      const client = new Patched({ apiKey: "test" });
      const http = httpOptionsOf(client);
      expect(typeof http.fetch).toBe("function");
      expect(http.fetch).not.toBe(options.baseFetch);
    } finally {
      instr.uninstrument();
    }
  });

  it("merges non-destructively — preserves other httpOptions", () => {
    const mod = fakeGoogleModule();
    mockState.module = mod;
    const { options } = makeCapturingInstrumentorOptions();
    const instr = new GoogleGenAIInstrumentor(options);
    instr.instrument();
    try {
      const Patched = mod.GoogleGenAI as new (o: Record<string, unknown>) => { config: Record<string, unknown> };
      const client = new Patched({
        apiKey: "test",
        httpOptions: { baseUrl: "https://custom.example" },
      });
      const http = httpOptionsOf(client);
      // Checkrd injected fetch WITHOUT clobbering the caller's baseUrl.
      expect(http.baseUrl).toBe("https://custom.example");
      expect(typeof http.fetch).toBe("function");
    } finally {
      instr.uninstrument();
    }
  });

  it("does not override an explicitly-supplied httpOptions.fetch", () => {
    const mod = fakeGoogleModule();
    mockState.module = mod;
    const { options } = makeCapturingInstrumentorOptions();
    const explicit = (async () => new Response("x")) as unknown as typeof fetch;
    const instr = new GoogleGenAIInstrumentor(options);
    instr.instrument();
    try {
      const Patched = mod.GoogleGenAI as new (o: Record<string, unknown>) => { config: Record<string, unknown> };
      const client = new Patched({ apiKey: "test", httpOptions: { fetch: explicit } });
      expect(httpOptionsOf(client).fetch).toBe(explicit);
    } finally {
      instr.uninstrument();
    }
  });

  it("drives a request through the wrapped fetch and emits telemetry", async () => {
    const mod = fakeGoogleModule();
    mockState.module = mod;
    const { options, events, fetchCalls } = makeCapturingInstrumentorOptions();
    const instr = new GoogleGenAIInstrumentor(options);
    instr.instrument();
    try {
      const Patched = mod.GoogleGenAI as new (o: Record<string, unknown>) => { config: Record<string, unknown> };
      const client = new Patched({ apiKey: "test" });
      const wrapped = httpOptionsOf(client).fetch as typeof fetch;
      const url = "https://generativelanguage.googleapis.com/v1beta/models/gemini:generateContent";
      const res = await wrapped(url);
      expect(res.status).toBe(200);
      expect(fetchCalls).toContain(url);
      expect(events.length).toBeGreaterThan(0);
      expect(events[0]).toMatchObject({ agent_id: "test-agent" });
    } finally {
      instr.uninstrument();
    }
  });

  it("no-ops (with a loud warning) when the module shape is unrecognised", () => {
    // Package present but `GoogleGenAI` is not a function — the shape
    // guard must refuse to patch and log the structural break.
    mockState.module = { GoogleGenAI: 123 };
    const warn = vi.fn();
    const instr = new GoogleGenAIInstrumentor({
      ...makeCapturingInstrumentorOptions().options,
      logger: { debug: vi.fn(), info: vi.fn(), warn, error: vi.fn() },
    });
    instr.instrument();
    try {
      expect((mockState.module as Record<string, unknown>).GoogleGenAI).toBe(123);
      expect(warn).toHaveBeenCalledWith(
        expect.stringContaining("@google/genai vendor SDK shape mismatch"),
        expect.objectContaining({ vendor: "@google/genai" }),
      );
    } finally {
      instr.uninstrument();
    }
  });

  it("uninstrument restores the original constructor", () => {
    const mod = fakeGoogleModule();
    mockState.module = mod;
    const original = mod.GoogleGenAI;
    const instr = new GoogleGenAIInstrumentor(makeCapturingInstrumentorOptions().options);
    instr.instrument();
    expect(mod.GoogleGenAI).not.toBe(original);
    instr.uninstrument();
    expect(mod.GoogleGenAI).toBe(original);
  });
});
