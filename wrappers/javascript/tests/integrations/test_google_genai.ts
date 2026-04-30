/**
 * Google GenAI instrumentor — `@google/genai` is an OPTIONAL peer dep,
 * not in devDependencies. Verifies the missing-package no-op contract.
 */
import { describe, expect, it } from "vitest";

import { GoogleGenAIInstrumentor } from "../../src/integrations/_google_genai.js";

import { makeInstrumentorOptions } from "./_helpers.js";

describe("GoogleGenAIInstrumentor (@google/genai not installed)", () => {
  it("instrument() is a silent no-op", () => {
    const instr = new GoogleGenAIInstrumentor(makeInstrumentorOptions());
    expect(() => { instr.instrument(); }).not.toThrow();
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
