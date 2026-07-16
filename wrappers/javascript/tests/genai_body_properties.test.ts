/**
 * Property-based tests for the GenAI body extractor.
 *
 * Companion to the golden-fixture parity test (`genai_fixtures.test.ts`,
 * which pins exact outputs) and a direct analogue of the Python
 * Hypothesis suite. fast-check fuzzes the three contracts that the
 * fixtures can only spot-check:
 *
 *   (a) never throws on arbitrary bytes / JSON objects / header bags;
 *   (b) the inclusion-rule invariant
 *       (`cache_read + cache_creation <= input`, `reasoning <= output`)
 *       holds whenever the detail counters are emitted;
 *   (c) Anthropic normalizes input to `raw_input + cache_read + cache_creation`.
 */
import fc from "fast-check";
import { describe, expect, it } from "vitest";

import {
  extractRequestAttrs,
  extractResponseAttrs,
} from "../src/_genai_body.js";

const ATTR_INPUT = "gen_ai.usage.input_tokens";
const ATTR_OUTPUT = "gen_ai.usage.output_tokens";
const ATTR_CACHE_READ = "gen_ai.usage.cache_read.input_tokens";
const ATTR_CACHE_CREATION = "gen_ai.usage.cache_creation.input_tokens";
const ATTR_REASONING = "gen_ai.usage.reasoning.output_tokens";

// Every provider the extractor routes, plus a couple it must ignore,
// so the fuzzer hits both the populated and the empty branches.
const providerArb = fc.constantFrom(
  "openai",
  "azure.openai",
  "anthropic",
  "aws.bedrock",
  "google.gemini",
  "google.vertex_ai",
  "cohere",
  "perplexity",
  "",
  undefined,
);

// Small integers, including 0 and negatives, so normalization /
// inclusion logic is exercised with realistic counts.
const tokenArb = fc.integer({ min: 0, max: 1_000_000 });

const headerRecordArb = fc.dictionary(
  fc.oneof(
    fc.constantFrom(
      "x-amzn-bedrock-input-token-count",
      "X-Amzn-Bedrock-Output-Token-Count",
      "content-type",
    ),
    fc.string(),
  ),
  fc.oneof(fc.string(), tokenArb.map((n) => String(n))),
);

describe("extractor never throws (robustness contract)", () => {
  it("survives arbitrary byte buffers for any provider", () => {
    fc.assert(
      fc.property(providerArb, fc.uint8Array(), headerRecordArb, (provider, bytes, headers) => {
        expect(() => extractRequestAttrs(provider, bytes)).not.toThrow();
        expect(() => extractResponseAttrs(provider, bytes, headers)).not.toThrow();
      }),
      { numRuns: 300 },
    );
  });

  it("survives arbitrary JSON objects encoded to bytes", () => {
    fc.assert(
      fc.property(providerArb, fc.object(), headerRecordArb, (provider, obj, headers) => {
        const bytes = new TextEncoder().encode(JSON.stringify(obj));
        expect(() => extractRequestAttrs(provider, bytes)).not.toThrow();
        expect(() => extractResponseAttrs(provider, bytes, headers)).not.toThrow();
      }),
      { numRuns: 300 },
    );
  });

  it("survives arbitrary JSON strings and header records", () => {
    fc.assert(
      fc.property(providerArb, fc.json(), headerRecordArb, (provider, jsonStr, headers) => {
        expect(() => extractRequestAttrs(provider, jsonStr)).not.toThrow();
        expect(() => extractResponseAttrs(provider, jsonStr, headers)).not.toThrow();
      }),
      { numRuns: 300 },
    );
  });

  it("always returns a plain object, never null/undefined", () => {
    fc.assert(
      fc.property(providerArb, fc.json(), (provider, jsonStr) => {
        const req = extractRequestAttrs(provider, jsonStr);
        const res = extractResponseAttrs(provider, jsonStr);
        expect(typeof req).toBe("object");
        expect(typeof res).toBe("object");
        expect(req).not.toBeNull();
        expect(res).not.toBeNull();
      }),
      { numRuns: 200 },
    );
  });
});

/** Read a numeric attribute, or undefined if absent/non-numeric. */
function num(attrs: Record<string, unknown>, key: string): number | undefined {
  const v = attrs[key];
  return typeof v === "number" ? v : undefined;
}

describe("inclusion-rule invariant (billing-critical)", () => {
  it("OpenAI: cache_read <= input and reasoning <= output whenever both emitted", () => {
    fc.assert(
      fc.property(
        tokenArb,
        tokenArb,
        tokenArb,
        tokenArb,
        (prompt, completion, cached, reasoning) => {
          // Realistic: cached ⊆ prompt, reasoning ⊆ completion (clamp
          // the detail counters so the body itself is well-formed; the
          // extractor passes OpenAI counts through without normalizing,
          // so a malformed body in would just propagate out).
          const body = {
            model: "gpt-4o",
            usage: {
              prompt_tokens: prompt,
              completion_tokens: completion,
              prompt_tokens_details: { cached_tokens: Math.min(cached, prompt) },
              completion_tokens_details: {
                reasoning_tokens: Math.min(reasoning, completion),
              },
            },
          };
          const attrs = extractResponseAttrs(
            "openai",
            new TextEncoder().encode(JSON.stringify(body)),
          );
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
      fc.property(tokenArb, tokenArb, tokenArb, tokenArb, (input, output, cacheRead, cacheCreation) => {
        const body = {
          model: "claude-sonnet-4-5",
          usage: {
            input_tokens: input,
            output_tokens: output,
            cache_read_input_tokens: cacheRead,
            cache_creation_input_tokens: cacheCreation,
          },
        };
        const attrs = extractResponseAttrs(
          "anthropic",
          new TextEncoder().encode(JSON.stringify(body)),
        );
        const emittedInput = num(attrs, ATTR_INPUT);
        const emittedCacheRead = num(attrs, ATTR_CACHE_READ);
        const emittedCacheCreation = num(attrs, ATTR_CACHE_CREATION);
        expect(emittedInput).toBeDefined();
        const detailSum =
          (emittedCacheRead ?? 0) + (emittedCacheCreation ?? 0);
        expect(detailSum).toBeLessThanOrEqual(emittedInput!);
      }),
      { numRuns: 300 },
    );
  });

  it("Gemini: cache_read <= input and reasoning <= output whenever emitted", () => {
    fc.assert(
      fc.property(tokenArb, tokenArb, tokenArb, tokenArb, (prompt, candidates, cached, thoughts) => {
        const body = {
          modelVersion: "gemini-2.5-pro",
          usageMetadata: {
            promptTokenCount: prompt,
            candidatesTokenCount: candidates,
            // cached ⊆ prompt, thoughts ⊆ candidates (semconv subset).
            cachedContentTokenCount: Math.min(cached, prompt),
            thoughtsTokenCount: Math.min(thoughts, candidates),
          },
        };
        const attrs = extractResponseAttrs(
          "google.gemini",
          new TextEncoder().encode(JSON.stringify(body)),
        );
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
      }),
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
          const usage: Record<string, number> = {
            input_tokens: rawInput,
            output_tokens: output,
          };
          if (cacheRead !== undefined) usage["cache_read_input_tokens"] = cacheRead;
          if (cacheCreation !== undefined) {
            usage["cache_creation_input_tokens"] = cacheCreation;
          }
          const body = { model: "claude-sonnet-4-5", usage };
          const attrs = extractResponseAttrs(
            "anthropic",
            new TextEncoder().encode(JSON.stringify(body)),
          );
          const expected = rawInput + (cacheRead ?? 0) + (cacheCreation ?? 0);
          expect(num(attrs, ATTR_INPUT)).toBe(expected);
        },
      ),
      { numRuns: 400 },
    );
  });
});
