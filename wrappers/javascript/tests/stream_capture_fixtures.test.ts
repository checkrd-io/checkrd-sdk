/**
 * Golden-fixture parity test for the streaming usage tap (M-11).
 *
 * Loads every case in `schemas/genai-fixtures/streaming/*.json` and
 * asserts the TS tap reproduces each `expected_usage_attrs` /
 * `expected_pricing_status` frame-for-frame. The Python streaming tap
 * is built greenfield against the SAME fixtures in parallel, so a field
 * that drifts between the two runtimes fails here in at least one of
 * them — this file IS the cross-runtime streaming parity contract.
 *
 * The fixtures provide `sse_frames` (raw SSE wire frames, in order) +
 * `complete` (false = stream abandoned before its terminal usage frame).
 * The pure `captureUsageFromFrames` seam takes exactly that shape, so we
 * can drive the tap in isolation without a live `ReadableStream`.
 */
import { readFileSync, readdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import {
  captureUsageFromFrames,
  type StreamVendor,
} from "../src/_stream_capture.js";

const here = dirname(fileURLToPath(import.meta.url));
// Fixtures live at repo-root `schemas/genai-fixtures/streaming/`; from
// `wrappers/javascript/tests` that is `../../../schemas/...`.
const FIXTURE_DIR = resolve(
  here,
  "..",
  "..",
  "..",
  "schemas",
  "genai-fixtures",
  "streaming",
);

interface StreamFixtureCase {
  name: string;
  provider: string;
  sse_frames: string[];
  complete: boolean;
  expected_usage_attrs: Record<string, number>;
  expected_pricing_status?: string;
}

function loadFixtures(file: string): StreamFixtureCase[] {
  const raw = readFileSync(join(FIXTURE_DIR, file), "utf-8");
  const parsed: unknown = JSON.parse(raw);
  if (!Array.isArray(parsed)) {
    throw new Error(`fixture ${file} is not a JSON array`);
  }
  return parsed as StreamFixtureCase[];
}

const fixtureFiles = readdirSync(FIXTURE_DIR)
  .filter((f) => f.endsWith(".json"))
  .sort();

// Guard against a silently empty fixture dir (path typo, bad checkout)
// — an empty sweep would make this whole suite vacuously green.
describe("streaming fixtures discovery", () => {
  it("found the streaming fixture files", () => {
    expect(fixtureFiles).toEqual(["anthropic.json", "openai.json"]);
  });
});

for (const file of fixtureFiles) {
  const cases = loadFixtures(file);
  describe(`streaming fixtures — ${file}`, () => {
    it("declares at least one case", () => {
      expect(cases.length).toBeGreaterThan(0);
    });

    for (const c of cases) {
      it(c.name, () => {
        const result = captureUsageFromFrames(
          c.provider as StreamVendor,
          c.sse_frames,
          c.complete,
        );
        // Usage attrs must match the fixture exactly — same keys, same
        // values, no extras (empty on an abandoned stream).
        expect(result.usageAttrs).toEqual(c.expected_usage_attrs);
        if (c.expected_pricing_status !== undefined) {
          expect(result.pricingStatus).toBe(c.expected_pricing_status);
        } else {
          // A tallied case must NOT carry a pricing status.
          expect(result.pricingStatus).toBeUndefined();
        }
      });
    }
  });
}

// ---------------------------------------------------------------------------
// Cross-vendor invariant spot-check from the fixtures: the worked
// Anthropic example (input 300 + cache_read 1000 + cache_creation 200 =
// 1500) is the billing-critical normalization the README pins. Assert it
// directly off the fixture so a regression in the sum surfaces by name.
// ---------------------------------------------------------------------------

describe("streaming fixtures — Anthropic inclusive-input normalization", () => {
  it("emits input = raw + cache_read + cache_creation (1500) for the cache case", () => {
    const c = loadFixtures("anthropic.json").find(
      (x) => x.name === "anthropic_stream_message_start_and_delta_with_cache",
    );
    expect(c, "cache fixture present").toBeDefined();
    const result = captureUsageFromFrames("anthropic", c!.sse_frames, c!.complete);
    expect(result.usageAttrs["gen_ai.usage.input_tokens"]).toBe(1500);
    expect(result.usageAttrs["gen_ai.usage.cache_read.input_tokens"]).toBe(1000);
    expect(result.usageAttrs["gen_ai.usage.cache_creation.input_tokens"]).toBe(200);
  });
});

// ---------------------------------------------------------------------------
// Abandonment parity: every `complete: false` fixture must yield empty
// usage + untallied, regardless of how much partial usage the earlier
// frames carried (e.g. Anthropic's `message_start` input_tokens). This
// is the §4.2 "engine never estimates" contract.
// ---------------------------------------------------------------------------

describe("streaming fixtures — abandonment yields untallied + empty usage", () => {
  const abandoned: { file: string; case: StreamFixtureCase }[] = [];
  for (const file of fixtureFiles) {
    for (const c of loadFixtures(file)) {
      if (!c.complete) abandoned.push({ file, case: c });
    }
  }

  it("found at least one abandoned fixture per vendor", () => {
    expect(abandoned.length).toBeGreaterThanOrEqual(2);
  });

  for (const { file, case: c } of abandoned) {
    it(`${file}:${c.name} → untallied`, () => {
      const result = captureUsageFromFrames(
        c.provider as StreamVendor,
        c.sse_frames,
        c.complete,
      );
      expect(result.usageAttrs).toEqual({});
      expect(result.pricingStatus).toBe("untallied");
    });
  }
});
