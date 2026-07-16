/**
 * Cohere instrumentor tests.
 *
 * `cohere-ai` is an OPTIONAL peer dep, intentionally NOT installed in
 * devDependencies (it is Node-only and heavy). Two contracts are
 * verified here:
 *
 *   1. **Missing-package no-op** — `instrument()` / `uninstrument()` are
 *      silent no-ops when the package is absent.
 *   2. **Real instrumentation** — the `new Proxy(..., { construct })`
 *      fetch-injection actually runs. Because the SDK can't be a
 *      devDependency, we mock `lazyRequireOptional` to hand the
 *      instrumentor a minimal module whose client shape matches
 *      `cohere-ai`'s (a `fetcher` option in the constructor bag). The
 *      REAL `patchModuleExport` still runs, so the construct trap, the
 *      export swap, and the revert are all exercised end-to-end.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { CohereInstrumentor } from "../../src/integrations/_cohere.js";

import { makeCapturingInstrumentorOptions, makeInstrumentorOptions } from "./_helpers.js";

// Hoisted holder the mock factory and the test body both see. When
// `module` is undefined the mocked require throws (package "absent");
// a test sets it to a fake `cohere-ai` module to go down the real path.
const mockState = vi.hoisted(() => ({ module: undefined as unknown }));

vi.mock("../../src/integrations/_require.js", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../src/integrations/_require.js")>();
  return {
    ...actual, // keep the REAL patchModuleExport
    lazyRequireOptional: () => (name: string): unknown => {
      if (name === "cohere-ai" && mockState.module !== undefined) return mockState.module;
      throw new Error(`cohere-ai not installed (mock): ${name}`);
    },
  };
});

/** A single captured constructor call's option bag. */
class FakeCohereClient {
  public readonly config: Record<string, unknown>;
  constructor(config: Record<string, unknown> = {}) {
    this.config = config;
  }
}
class FakeCohereClientV2 {
  public readonly config: Record<string, unknown>;
  constructor(config: Record<string, unknown> = {}) {
    this.config = config;
  }
}

/** Build a fresh fake `cohere-ai` module for one test. */
function fakeCohereModule(): { CohereClient: unknown; CohereClientV2: unknown } {
  return { CohereClient: FakeCohereClient, CohereClientV2: FakeCohereClientV2 };
}

afterEach(() => {
  mockState.module = undefined;
});

describe("CohereInstrumentor (cohere-ai not installed)", () => {
  it("instrument() is a silent no-op", () => {
    const instr = new CohereInstrumentor(makeInstrumentorOptions());
    expect(() => { instr.instrument(); }).not.toThrow();
    instr.uninstrument();
  });

  it("uninstrument() is a silent no-op", () => {
    const instr = new CohereInstrumentor(makeInstrumentorOptions());
    expect(() => { instr.uninstrument(); }).not.toThrow();
  });

  it("is idempotent under repeated calls", () => {
    const instr = new CohereInstrumentor(makeInstrumentorOptions());
    instr.instrument();
    instr.instrument();
    expect(instr.isInstalled).toBe(true);
    instr.uninstrument();
    instr.uninstrument();
    expect(instr.isInstalled).toBe(false);
  });
});

describe("CohereInstrumentor (real instrumentation)", () => {
  it("injects the wrapped fetch as `fetcher` on a new client", () => {
    const mod = fakeCohereModule();
    mockState.module = mod;
    const { options } = makeCapturingInstrumentorOptions();
    const instr = new CohereInstrumentor(options);
    instr.instrument();
    try {
      const Patched = mod.CohereClient as new (o: Record<string, unknown>) => { config: Record<string, unknown> };
      const client = new Patched({ token: "test" });
      // Cohere's v7 SDK reads `fetcher`, NOT `fetch`.
      expect(typeof client.config.fetcher).toBe("function");
      // It must be the CHECKRD-wrapped fetch, not the raw base fetch.
      expect(client.config.fetcher).not.toBe(options.baseFetch);
      // Caller-supplied config survives the merge.
      expect(client.config.token).toBe("test");
    } finally {
      instr.uninstrument();
    }
  });

  it("patches CohereClientV2 as well", () => {
    const mod = fakeCohereModule();
    mockState.module = mod;
    const { options } = makeCapturingInstrumentorOptions();
    const instr = new CohereInstrumentor(options);
    instr.instrument();
    try {
      const Patched = mod.CohereClientV2 as new (o: Record<string, unknown>) => { config: Record<string, unknown> };
      const client = new Patched({ token: "test" });
      expect(typeof client.config.fetcher).toBe("function");
    } finally {
      instr.uninstrument();
    }
  });

  it("does not override an explicitly-supplied fetcher", () => {
    const mod = fakeCohereModule();
    mockState.module = mod;
    const { options } = makeCapturingInstrumentorOptions();
    const explicit = (async () => new Response("x")) as unknown as typeof fetch;
    const instr = new CohereInstrumentor(options);
    instr.instrument();
    try {
      const Patched = mod.CohereClient as new (o: Record<string, unknown>) => { config: Record<string, unknown> };
      const client = new Patched({ token: "test", fetcher: explicit });
      expect(client.config.fetcher).toBe(explicit);
    } finally {
      instr.uninstrument();
    }
  });

  it("drives a request through the wrapped fetch and emits telemetry", async () => {
    const mod = fakeCohereModule();
    mockState.module = mod;
    const { options, events, fetchCalls } = makeCapturingInstrumentorOptions();
    const instr = new CohereInstrumentor(options);
    instr.instrument();
    try {
      const Patched = mod.CohereClient as new (o: Record<string, unknown>) => { config: Record<string, unknown> };
      const client = new Patched({ token: "test" });
      const wrapped = client.config.fetcher as typeof fetch;
      const res = await wrapped("https://api.cohere.com/v1/chat");
      // Request flowed through to the base fetch...
      expect(res.status).toBe(200);
      expect(fetchCalls).toContain("https://api.cohere.com/v1/chat");
      // ...and Checkrd emitted a telemetry event for it.
      expect(events.length).toBeGreaterThan(0);
      expect(events[0]).toMatchObject({ agent_id: "test-agent" });
    } finally {
      instr.uninstrument();
    }
  });

  it("no-ops (with a loud warning) when the module shape is unrecognised", () => {
    // Package present but neither candidate export is a function — the
    // shape guard must refuse to patch and log the structural break
    // rather than silently losing instrumentation in production.
    mockState.module = { CohereClient: 123, CohereClientV2: "nope" };
    const warn = vi.fn();
    const instr = new CohereInstrumentor({
      ...makeCapturingInstrumentorOptions().options,
      logger: { debug: vi.fn(), info: vi.fn(), warn, error: vi.fn() },
    });
    instr.instrument();
    try {
      // Exports left untouched — nothing was patched.
      expect((mockState.module as Record<string, unknown>).CohereClient).toBe(123);
      expect(warn).toHaveBeenCalledWith(
        expect.stringContaining("cohere-ai vendor SDK shape mismatch"),
        expect.objectContaining({ vendor: "cohere-ai" }),
      );
    } finally {
      instr.uninstrument();
    }
  });

  it("skips a client whose export is sealed (patch cannot be applied)", () => {
    // Some bundlers freeze module namespaces. patchModuleExport then
    // fails; the instrumentor must leave the export untouched (not a
    // half-applied Proxy) rather than throw.
    const mod: Record<string, unknown> = { CohereClientV2: FakeCohereClientV2 };
    Object.defineProperty(mod, "CohereClient", {
      value: FakeCohereClient,
      writable: false,
      configurable: false,
      enumerable: true,
    });
    mockState.module = mod;
    const instr = new CohereInstrumentor(makeCapturingInstrumentorOptions().options);
    instr.instrument();
    try {
      // Sealed export left as the original class — not swapped for a Proxy.
      expect(mod.CohereClient).toBe(FakeCohereClient);
      // The writable sibling still got patched.
      expect(mod.CohereClientV2).not.toBe(FakeCohereClientV2);
    } finally {
      instr.uninstrument();
    }
  });

  it("uninstrument restores the original constructor", () => {
    const mod = fakeCohereModule();
    mockState.module = mod;
    const original = mod.CohereClient;
    const instr = new CohereInstrumentor(makeCapturingInstrumentorOptions().options);
    instr.instrument();
    expect(mod.CohereClient).not.toBe(original); // proxied
    instr.uninstrument();
    expect(mod.CohereClient).toBe(original); // restored
  });
});
