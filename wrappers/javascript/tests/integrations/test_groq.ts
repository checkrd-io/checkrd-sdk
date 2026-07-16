/**
 * Groq instrumentor tests.
 *
 * `groq-sdk` is an OPTIONAL peer dep, not in devDependencies. Two
 * contracts are verified:
 *
 *   1. **Missing-package no-op** — silent no-op when the package is
 *      absent.
 *   2. **Real instrumentation** — the construct-trap fetch-injection
 *      runs. Since the SDK can't be a devDependency, we mock
 *      `lazyRequireOptional` to hand the instrumentor a minimal module
 *      matching `groq-sdk`'s shape (client accepts a `fetch` option,
 *      exported as both `Groq` and `default`). The real
 *      `patchModuleExport` still runs.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { GroqInstrumentor } from "../../src/integrations/_groq.js";

import { makeCapturingInstrumentorOptions, makeInstrumentorOptions } from "./_helpers.js";

const mockState = vi.hoisted(() => ({ module: undefined as unknown }));

vi.mock("../../src/integrations/_require.js", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../src/integrations/_require.js")>();
  return {
    ...actual, // keep the REAL patchModuleExport
    lazyRequireOptional: () => (name: string): unknown => {
      if (name === "groq-sdk" && mockState.module !== undefined) return mockState.module;
      throw new Error(`groq-sdk not installed (mock): ${name}`);
    },
  };
});

class FakeGroq {
  public readonly config: Record<string, unknown>;
  constructor(config: Record<string, unknown> = {}) {
    this.config = config;
  }
}

/**
 * Fresh fake `groq-sdk` module. The SDK ships its client as both a
 * named `Groq` export and the module `default`, so we mirror both.
 */
function fakeGroqModule(): { Groq: unknown; default: unknown } {
  return { Groq: FakeGroq, default: FakeGroq };
}

afterEach(() => {
  mockState.module = undefined;
});

describe("GroqInstrumentor (groq-sdk not installed)", () => {
  it("instrument() is a silent no-op", () => {
    const instr = new GroqInstrumentor(makeInstrumentorOptions());
    expect(() => { instr.instrument(); }).not.toThrow();
    instr.uninstrument();
  });

  it("uninstrument() is a silent no-op", () => {
    const instr = new GroqInstrumentor(makeInstrumentorOptions());
    expect(() => { instr.uninstrument(); }).not.toThrow();
  });

  it("is idempotent under repeated calls", () => {
    const instr = new GroqInstrumentor(makeInstrumentorOptions());
    instr.instrument();
    instr.instrument();
    expect(instr.isInstalled).toBe(true);
    instr.uninstrument();
    instr.uninstrument();
    expect(instr.isInstalled).toBe(false);
  });
});

describe("GroqInstrumentor (real instrumentation)", () => {
  it("injects the wrapped fetch as `fetch` on a new client", () => {
    const mod = fakeGroqModule();
    mockState.module = mod;
    const { options } = makeCapturingInstrumentorOptions();
    const instr = new GroqInstrumentor(options);
    instr.instrument();
    try {
      const Patched = mod.Groq as new (o: Record<string, unknown>) => { config: Record<string, unknown> };
      const client = new Patched({ apiKey: "test" });
      expect(typeof client.config.fetch).toBe("function");
      expect(client.config.fetch).not.toBe(options.baseFetch);
      expect(client.config.apiKey).toBe("test");
    } finally {
      instr.uninstrument();
    }
  });

  it("patches the module `default` export too", () => {
    const mod = fakeGroqModule();
    mockState.module = mod;
    const { options } = makeCapturingInstrumentorOptions();
    const instr = new GroqInstrumentor(options);
    instr.instrument();
    try {
      const Patched = mod.default as new (o: Record<string, unknown>) => { config: Record<string, unknown> };
      const client = new Patched({ apiKey: "test" });
      expect(typeof client.config.fetch).toBe("function");
    } finally {
      instr.uninstrument();
    }
  });

  it("does not override an explicitly-supplied fetch", () => {
    const mod = fakeGroqModule();
    mockState.module = mod;
    const { options } = makeCapturingInstrumentorOptions();
    const explicit = (async () => new Response("x")) as unknown as typeof fetch;
    const instr = new GroqInstrumentor(options);
    instr.instrument();
    try {
      const Patched = mod.Groq as new (o: Record<string, unknown>) => { config: Record<string, unknown> };
      const client = new Patched({ apiKey: "test", fetch: explicit });
      expect(client.config.fetch).toBe(explicit);
    } finally {
      instr.uninstrument();
    }
  });

  it("drives a request through the wrapped fetch and emits telemetry", async () => {
    const mod = fakeGroqModule();
    mockState.module = mod;
    const { options, events, fetchCalls } = makeCapturingInstrumentorOptions();
    const instr = new GroqInstrumentor(options);
    instr.instrument();
    try {
      const Patched = mod.Groq as new (o: Record<string, unknown>) => { config: Record<string, unknown> };
      const client = new Patched({ apiKey: "test" });
      const wrapped = client.config.fetch as typeof fetch;
      const res = await wrapped("https://api.groq.com/openai/v1/chat/completions");
      expect(res.status).toBe(200);
      expect(fetchCalls).toContain("https://api.groq.com/openai/v1/chat/completions");
      expect(events.length).toBeGreaterThan(0);
      expect(events[0]).toMatchObject({ agent_id: "test-agent" });
    } finally {
      instr.uninstrument();
    }
  });

  it("no-ops (with a loud warning) when the module shape is unrecognised", () => {
    // Package present but neither candidate export is a function — the
    // shape guard must refuse to patch and log the structural break.
    mockState.module = { Groq: 123, default: "nope" };
    const warn = vi.fn();
    const instr = new GroqInstrumentor({
      ...makeCapturingInstrumentorOptions().options,
      logger: { debug: vi.fn(), info: vi.fn(), warn, error: vi.fn() },
    });
    instr.instrument();
    try {
      expect((mockState.module as Record<string, unknown>).Groq).toBe(123);
      expect(warn).toHaveBeenCalledWith(
        expect.stringContaining("groq-sdk vendor SDK shape mismatch"),
        expect.objectContaining({ vendor: "groq-sdk" }),
      );
    } finally {
      instr.uninstrument();
    }
  });

  it("skips a client whose export is sealed (patch cannot be applied)", () => {
    // A frozen module namespace makes patchModuleExport fail; the
    // instrumentor must leave the export untouched, not throw.
    const mod: Record<string, unknown> = { default: FakeGroq };
    Object.defineProperty(mod, "Groq", {
      value: FakeGroq,
      writable: false,
      configurable: false,
      enumerable: true,
    });
    mockState.module = mod;
    const instr = new GroqInstrumentor(makeCapturingInstrumentorOptions().options);
    instr.instrument();
    try {
      expect(mod.Groq).toBe(FakeGroq); // sealed → untouched
      expect(mod.default).not.toBe(FakeGroq); // writable sibling patched
    } finally {
      instr.uninstrument();
    }
  });

  it("uninstrument restores the original constructor", () => {
    const mod = fakeGroqModule();
    mockState.module = mod;
    const original = mod.Groq;
    const instr = new GroqInstrumentor(makeCapturingInstrumentorOptions().options);
    instr.instrument();
    expect(mod.Groq).not.toBe(original);
    instr.uninstrument();
    expect(mod.Groq).toBe(original);
  });
});
