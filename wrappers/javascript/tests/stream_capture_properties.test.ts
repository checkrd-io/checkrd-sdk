/**
 * Property-based tests for the streaming usage tap (M-11).
 *
 * Companion to the golden-fixture parity test
 * (`stream_capture_fixtures.test.ts`, which pins exact outputs) and a
 * direct analogue of the Python Hypothesis suite. fast-check fuzzes the
 * four contracts the fixtures can only spot-check:
 *
 *   (a) never throws on arbitrary frame arrays (malformed-frame
 *       robustness — the tap must never crash the consumer's stream);
 *   (b) the inclusion-rule invariant on captured usage
 *       (`cache_read + cache_creation <= input`, `reasoning <= output`);
 *   (c) Anthropic normalization equality
 *       (`input == raw + cache_read + cache_creation`);
 *   (d) an incomplete stream ALWAYS yields untallied + empty usage,
 *       no matter what partial frames preceded the cut-off.
 */
import fc from "fast-check";
import { describe, expect, it } from "vitest";

import {
  captureUsageFromFrames,
  type StreamVendor,
} from "../src/_stream_capture.js";

const ATTR_INPUT = "gen_ai.usage.input_tokens";
const ATTR_OUTPUT = "gen_ai.usage.output_tokens";
const ATTR_CACHE_READ = "gen_ai.usage.cache_read.input_tokens";
const ATTR_CACHE_CREATION = "gen_ai.usage.cache_creation.input_tokens";
const ATTR_REASONING = "gen_ai.usage.reasoning.output_tokens";

const vendorArb: fc.Arbitrary<StreamVendor> = fc.constantFrom(
  "openai",
  "anthropic",
  "unknown",
);

// Non-negative integers, realistic token magnitudes.
const tokenArb = fc.integer({ min: 0, max: 1_000_000 });

/** Read a numeric attribute, or undefined if absent/non-numeric. */
function num(attrs: Record<string, number>, key: string): number | undefined {
  const v = attrs[key];
  return typeof v === "number" ? v : undefined;
}

// --- Frame builders matching the fixture wire shapes -----------------------

function openAIUsageFrame(
  prompt: number,
  completion: number,
  cached?: number,
  reasoning?: number,
): string {
  const usage: Record<string, unknown> = {
    prompt_tokens: prompt,
    completion_tokens: completion,
  };
  if (cached !== undefined) usage["prompt_tokens_details"] = { cached_tokens: cached };
  if (reasoning !== undefined) {
    usage["completion_tokens_details"] = { reasoning_tokens: reasoning };
  }
  return `data: ${JSON.stringify({ choices: [], usage })}\n\n`;
}

function anthropicStartFrame(
  input: number,
  cacheRead?: number,
  cacheCreation?: number,
): string {
  const usage: Record<string, unknown> = { input_tokens: input, output_tokens: 1 };
  if (cacheRead !== undefined) usage["cache_read_input_tokens"] = cacheRead;
  if (cacheCreation !== undefined) usage["cache_creation_input_tokens"] = cacheCreation;
  return `event: message_start\ndata: ${JSON.stringify({ type: "message_start", message: { usage } })}\n\n`;
}

function anthropicDeltaFrame(output: number): string {
  return `event: message_delta\ndata: ${JSON.stringify({ type: "message_delta", delta: { stop_reason: "end_turn" }, usage: { output_tokens: output } })}\n\n`;
}

// Arbitrary SSE-ish frames: a mix of valid-looking frames, junk, and
// fragments, so the parser's tolerance is exercised broadly.
const frameArb: fc.Arbitrary<string> = fc.oneof(
  fc.string().map((s) => `data: ${s}\n\n`),
  fc.string().map((s) => `event: ${s}\ndata: ${s}\n\n`),
  fc.json().map((j) => `data: ${j}\n\n`),
  fc.constant("data: [DONE]\n\n"),
  fc.constant(": comment\n\n"),
  fc.string(),
  // Occasionally a well-formed usage frame so the success path mixes in.
  fc
    .tuple(tokenArb, tokenArb)
    .map(([p, c]) => openAIUsageFrame(p, c)),
);

describe("tap never throws (robustness contract)", () => {
  it("survives arbitrary frame arrays for any vendor", () => {
    fc.assert(
      fc.property(
        vendorArb,
        fc.array(frameArb, { maxLength: 20 }),
        fc.boolean(),
        (vendor, frames, complete) => {
          expect(() => captureUsageFromFrames(vendor, frames, complete)).not.toThrow();
        },
      ),
      { numRuns: 400 },
    );
  });

  it("always returns a plain object usageAttrs, never null/undefined", () => {
    fc.assert(
      fc.property(
        vendorArb,
        fc.array(frameArb, { maxLength: 12 }),
        fc.boolean(),
        (vendor, frames, complete) => {
          const r = captureUsageFromFrames(vendor, frames, complete);
          expect(typeof r.usageAttrs).toBe("object");
          expect(r.usageAttrs).not.toBeNull();
        },
      ),
      { numRuns: 250 },
    );
  });
});

describe("inclusion-rule invariant on captured usage (billing-critical)", () => {
  it("OpenAI: cache_read <= input and reasoning <= output whenever both emitted", () => {
    fc.assert(
      fc.property(
        tokenArb,
        tokenArb,
        tokenArb,
        tokenArb,
        (prompt, completion, cached, reasoning) => {
          // cached ⊆ prompt, reasoning ⊆ completion (OpenAI counts are
          // native-inclusive; the tap passes them through unnormalized).
          const frames = [
            openAIUsageFrame(
              prompt,
              completion,
              Math.min(cached, prompt),
              Math.min(reasoning, completion),
            ),
            "data: [DONE]\n\n",
          ];
          const attrs = captureUsageFromFrames("openai", frames, true).usageAttrs;
          const input = num(attrs, ATTR_INPUT);
          const output = num(attrs, ATTR_OUTPUT);
          const cacheRead = num(attrs, ATTR_CACHE_READ);
          const reason = num(attrs, ATTR_REASONING);
          if (cacheRead !== undefined && input !== undefined) {
            expect(cacheRead).toBeLessThanOrEqual(input);
          }
          if (reason !== undefined && output !== undefined) {
            expect(reason).toBeLessThanOrEqual(output);
          }
        },
      ),
      { numRuns: 300 },
    );
  });

  it("Anthropic: cache_read + cache_creation <= input (post-normalization)", () => {
    fc.assert(
      fc.property(
        tokenArb,
        tokenArb,
        tokenArb,
        tokenArb,
        (input, output, cacheRead, cacheCreation) => {
          const frames = [
            anthropicStartFrame(input, cacheRead, cacheCreation),
            anthropicDeltaFrame(output),
          ];
          const attrs = captureUsageFromFrames("anthropic", frames, true).usageAttrs;
          const emittedInput = num(attrs, ATTR_INPUT);
          expect(emittedInput).toBeDefined();
          const detailSum =
            (num(attrs, ATTR_CACHE_READ) ?? 0) +
            (num(attrs, ATTR_CACHE_CREATION) ?? 0);
          expect(detailSum).toBeLessThanOrEqual(emittedInput!);
        },
      ),
      { numRuns: 300 },
    );
  });
});

describe("Anthropic normalization: input == raw + cache_read + cache_creation", () => {
  it("emitted input always equals the inclusive sum of the raw terms", () => {
    fc.assert(
      fc.property(
        tokenArb,
        tokenArb,
        // cache terms optionally present, to cover the missing-term-as-0 path.
        fc.option(tokenArb, { nil: undefined }),
        fc.option(tokenArb, { nil: undefined }),
        (rawInput, output, cacheRead, cacheCreation) => {
          const frames = [
            anthropicStartFrame(rawInput, cacheRead, cacheCreation),
            anthropicDeltaFrame(output),
          ];
          const attrs = captureUsageFromFrames("anthropic", frames, true).usageAttrs;
          const expected = rawInput + (cacheRead ?? 0) + (cacheCreation ?? 0);
          expect(num(attrs, ATTR_INPUT)).toBe(expected);
          expect(num(attrs, ATTR_OUTPUT)).toBe(output);
        },
      ),
      { numRuns: 400 },
    );
  });
});

describe("incomplete stream => untallied + empty usage, always", () => {
  it("complete=false yields untallied even with a full terminal usage frame", () => {
    fc.assert(
      fc.property(
        vendorArb,
        fc.array(frameArb, { maxLength: 15 }),
        (vendor, frames) => {
          const r = captureUsageFromFrames(vendor, frames, false);
          expect(r.usageAttrs).toEqual({});
          expect(r.pricingStatus).toBe("untallied");
        },
      ),
      { numRuns: 400 },
    );
  });

  it("OpenAI: dropping the include_usage frame => untallied (no estimate)", () => {
    fc.assert(
      fc.property(
        fc.array(fc.string(), { minLength: 0, maxLength: 8 }),
        (contents) => {
          // Content-only frames, no usage chunk — the abandoned shape.
          const frames = contents.map(
            (t) => `data: ${JSON.stringify({ choices: [{ delta: { content: t } }] })}\n\n`,
          );
          const r = captureUsageFromFrames("openai", frames, true);
          expect(r.usageAttrs).toEqual({});
          expect(r.pricingStatus).toBe("untallied");
        },
      ),
      { numRuns: 200 },
    );
  });

  it("Anthropic: message_start but no terminal message_delta => untallied", () => {
    fc.assert(
      fc.property(tokenArb, tokenArb, tokenArb, (input, cacheRead, cacheCreation) => {
        // Input usage present (incl. cache) but output never finalized.
        const frames = [
          anthropicStartFrame(input, cacheRead, cacheCreation),
          `event: content_block_delta\ndata: ${JSON.stringify({ type: "content_block_delta", index: 0, delta: { type: "text_delta", text: "x" } })}\n\n`,
        ];
        const r = captureUsageFromFrames("anthropic", frames, true);
        expect(r.usageAttrs).toEqual({});
        expect(r.pricingStatus).toBe("untallied");
      }),
      { numRuns: 250 },
    );
  });
});
