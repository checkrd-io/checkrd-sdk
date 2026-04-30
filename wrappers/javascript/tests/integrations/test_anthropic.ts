/**
 * Anthropic instrumentor lifecycle tests. `@anthropic-ai/sdk` is a
 * devDependency so the real patching path is exercised here.
 */
import { describe, expect, it } from "vitest";

import { AnthropicInstrumentor } from "../../src/integrations/_anthropic.js";

import { makeInstrumentorOptions } from "./_helpers.js";

describe("AnthropicInstrumentor", () => {
  it("instrument() / uninstrument() do not throw", () => {
    const instr = new AnthropicInstrumentor(makeInstrumentorOptions());
    expect(() => { instr.instrument(); }).not.toThrow();
    expect(() => { instr.uninstrument(); }).not.toThrow();
  });

  it("is idempotent across repeated instrument() / uninstrument()", () => {
    const instr = new AnthropicInstrumentor(makeInstrumentorOptions());
    instr.instrument();
    instr.instrument();
    expect(instr.isInstalled).toBe(true);
    instr.uninstrument();
    instr.uninstrument();
    expect(instr.isInstalled).toBe(false);
  });

  it("patches the default-export constructor to inject the wrapped fetch", async () => {
    const mod = (await import("@anthropic-ai/sdk")) as unknown as {
      default: new (opts: Record<string, unknown>) => { fetch?: typeof fetch };
    };
    const instr = new AnthropicInstrumentor(makeInstrumentorOptions());
    instr.instrument();
    try {
      const client = new mod.default({ apiKey: "test" });
      expect(client.fetch).toBeDefined();
    } finally {
      instr.uninstrument();
    }
  });

});
