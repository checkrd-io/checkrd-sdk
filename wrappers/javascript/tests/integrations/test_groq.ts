/**
 * Groq instrumentor — `groq-sdk` is an OPTIONAL peer dep, not in
 * devDependencies. Verifies the missing-package no-op contract.
 */
import { describe, expect, it } from "vitest";

import { GroqInstrumentor } from "../../src/integrations/_groq.js";

import { makeInstrumentorOptions } from "./_helpers.js";

describe("GroqInstrumentor (groq-sdk not installed)", () => {
  it("instrument() is a silent no-op", () => {
    const instr = new GroqInstrumentor(makeInstrumentorOptions());
    expect(() => { instr.instrument(); }).not.toThrow();
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
