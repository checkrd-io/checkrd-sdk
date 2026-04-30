/**
 * Cohere instrumentor — `cohere-ai` is an OPTIONAL peer dep, intentionally
 * not installed in devDependencies. The contract is that instrument()
 * and uninstrument() are silent no-ops when the package is missing.
 */
import { describe, expect, it } from "vitest";

import { CohereInstrumentor } from "../../src/integrations/_cohere.js";

import { makeInstrumentorOptions } from "./_helpers.js";

describe("CohereInstrumentor (cohere-ai not installed)", () => {
  it("instrument() is a silent no-op", () => {
    const instr = new CohereInstrumentor(makeInstrumentorOptions());
    expect(() => { instr.instrument(); }).not.toThrow();
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
