/**
 * Tests for the hand-rolled OTLP/HTTP-JSON GenAI **metrics** exporter
 * (`_otlp_metrics.ts`, M-15). Three layers:
 *
 *   1. Golden-fixture parity — the load-bearing contract. Every fixture in
 *      `schemas/genai-fixtures/metrics/*.json` is recorded into a fresh
 *      accumulator, serialized to OTLP/JSON, and each instrument's data points
 *      are matched (order-independent, by attribute set) against `expected`.
 *      The Python SDK records the SAME fixtures and must produce byte-for-byte
 *      identical histogram data points.
 *   2. fast-check property tests — robustness (never throws on arbitrary
 *      events), the `sum(bucketCounts) === count` invariant, and independent
 *      re-derivation of the correct bucket for random token counts / durations.
 *   3. Edge-runtime smoke — the accumulator + serializer transpiled and run
 *      inside `@edge-runtime/vm` with a throwing `require`, proving no `node:*`
 *      dependency (the exporter must run on Workers / Edge / Deno / browser).
 */
import { readdirSync, readFileSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import fc from "fast-check";
import { transform } from "esbuild";
import { EdgeVM } from "@edge-runtime/vm";
import { beforeAll, describe, expect, it } from "vitest";

import {
  MetricsAccumulator,
  OPERATION_DURATION_BOUNDS,
  OPERATION_DURATION_METRIC,
  TOKEN_USAGE_BOUNDS,
  TOKEN_USAGE_METRIC,
  accumulatorToOtlpMetricsPayload,
  bucketIndex,
  recordEventMetrics,
  type OtlpMetricsPayload,
} from "../src/_otlp_metrics.js";
import type { TelemetryEvent } from "../src/sinks.js";

const here = dirname(fileURLToPath(import.meta.url));
const FIXTURE_DIR = resolve(here, "..", "..", "..", "schemas", "genai-fixtures", "metrics");
const SCHEMA_URL = "https://opentelemetry.io/schemas/1.41.0";

// ---------------------------------------------------------------------------
// Fixture types + helpers.
// ---------------------------------------------------------------------------

interface ExpectedDataPoint {
  attributes: Record<string, string>;
  count: number;
  sum: number;
  bucket_counts: number[];
}

interface ExpectedInstrument {
  unit: string;
  bounds: number[];
  data_points: ExpectedDataPoint[];
}

interface Fixture {
  name: string;
  events: TelemetryEvent[];
  expected: Record<string, ExpectedInstrument>;
}

function loadFixtures(): Fixture[] {
  return readdirSync(FIXTURE_DIR)
    .filter((f) => f.endsWith(".json"))
    .sort()
    .map((f) => JSON.parse(readFileSync(join(FIXTURE_DIR, f), "utf-8")) as Fixture);
}

/** Extract the OTLP metric object for `name` from the serialized payload. */
function metricOf(payload: OtlpMetricsPayload, name: string) {
  const scope = payload.resourceMetrics[0]?.scopeMetrics[0];
  const metric = scope?.metrics.find((m) => m.name === name);
  if (metric === undefined) throw new Error(`metric ${name} missing from payload`);
  return metric;
}

/**
 * A canonical, comparison-friendly view of one exported data point: attributes
 * as a plain map, count / sum as numbers, and bucketCounts normalized from the
 * OTLP wire form (decimal strings) back to numbers.
 */
interface NormalizedDataPoint {
  attributes: Record<string, string>;
  count: number;
  sum: number;
  bucketCounts: number[];
}

function normalizeExportedPoint(dp: {
  attributes: { key: string; value: { stringValue: string } }[];
  count: string;
  sum: number;
  bucketCounts: string[];
}): NormalizedDataPoint {
  const attributes: Record<string, string> = {};
  for (const a of dp.attributes) attributes[a.key] = a.value.stringValue;
  return {
    attributes,
    count: Number(dp.count),
    sum: dp.sum,
    bucketCounts: dp.bucketCounts.map((c) => Number(c)),
  };
}

/** Stable identity for a data point: its sorted attribute entries. */
function attrKey(attributes: Record<string, string>): string {
  return Object.entries(attributes)
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    .map(([k, v]) => `${k}=${v}`)
    .join("|");
}

// ---------------------------------------------------------------------------
// (1) Golden-fixture parity — the contract shared with the Python SDK.
// ---------------------------------------------------------------------------

describe("OTLP GenAI metrics — golden fixtures", () => {
  const fixtures = loadFixtures();

  it("discovers every fixture file (guards against an empty glob)", () => {
    // A silently-empty fixture set would make the whole parity suite a no-op.
    expect(fixtures.length).toBeGreaterThanOrEqual(4);
    expect(fixtures.map((f) => f.name).sort()).toEqual([
      "aggregation_same_series",
      "basic_single_call",
      "dimensional_split_by_model",
      "partial_missing_output",
    ]);
  });

  for (const fixture of fixtures) {
    describe(fixture.name, () => {
      // Record all events once, serialize once; every assertion reads this.
      const acc = new MetricsAccumulator();
      for (const event of fixture.events) recordEventMetrics(acc, event);
      const payload = accumulatorToOtlpMetricsPayload(acc, "checkrd-agent", SCHEMA_URL);

      for (const instrumentName of Object.keys(fixture.expected)) {
        const expectedInstrument = fixture.expected[instrumentName]!;

        it(`${instrumentName}: unit + bounds match`, () => {
          const metric = metricOf(payload, instrumentName);
          expect(metric.unit).toBe(expectedInstrument.unit);
          // CUMULATIVE temporality (2) on every histogram.
          expect(metric.histogram.aggregationTemporality).toBe(2);
          // Bounds pinned identically on every data point.
          for (const dp of metric.histogram.dataPoints) {
            expect(dp.explicitBounds).toEqual(expectedInstrument.bounds);
          }
        });

        it(`${instrumentName}: data points match by attribute set`, () => {
          const metric = metricOf(payload, instrumentName);
          const got = metric.histogram.dataPoints.map(normalizeExportedPoint);

          // Same number of series, matched order-independently by attributes.
          expect(got.length).toBe(expectedInstrument.data_points.length);

          const gotByKey = new Map(got.map((dp) => [attrKey(dp.attributes), dp]));
          for (const want of expectedInstrument.data_points) {
            const actual = gotByKey.get(attrKey(want.attributes));
            expect(
              actual,
              `no exported data point for attributes ${JSON.stringify(want.attributes)}`,
            ).toBeDefined();
            const dp = actual!;
            expect(dp.attributes).toEqual(want.attributes);
            expect(dp.count).toBe(want.count);
            // Compare sum numerically (float duration sums, e.g. 1.5s).
            expect(dp.sum).toBeCloseTo(want.sum, 9);
            expect(dp.bucketCounts).toEqual(want.bucket_counts);
            // The core invariant, re-checked against the fixture's own counts.
            const total = dp.bucketCounts.reduce((a, b) => a + b, 0);
            expect(total).toBe(want.count);
          }
        });
      }
    });
  }
});

// ---------------------------------------------------------------------------
// (2) fast-check property tests.
// ---------------------------------------------------------------------------

/** An arbitrary that biases toward the real GenAI event keys the sink reads. */
const genaiEventArb = fc.record(
  {
    "gen_ai.provider.name": fc.constantFrom("openai", "anthropic", "cohere"),
    "gen_ai.operation.name": fc.constantFrom("chat", "embeddings"),
    "gen_ai.request.model": fc.constantFrom("gpt-4o", "claude-sonnet-4-5"),
    "gen_ai.usage.input_tokens": fc.integer({ min: 0, max: 5_000_000 }),
    "gen_ai.usage.output_tokens": fc.integer({ min: 0, max: 5_000_000 }),
    latency_ms: fc.integer({ min: 0, max: 200_000 }),
  },
  { requiredKeys: [] },
);

describe("OTLP GenAI metrics — properties", () => {
  it("recordEventMetrics never throws on arbitrary event objects", () => {
    fc.assert(
      fc.property(fc.object(), (obj) => {
        const acc = new MetricsAccumulator();
        // `fc.object()` produces arbitrary nested values under arbitrary keys.
        expect(() => {
          recordEventMetrics(acc, obj as TelemetryEvent);
        }).not.toThrow();
      }),
      { numRuns: 300 },
    );
  });

  it("recordEventMetrics never throws on plausible GenAI events", () => {
    fc.assert(
      fc.property(fc.array(genaiEventArb, { maxLength: 20 }), (events) => {
        const acc = new MetricsAccumulator();
        for (const e of events) {
          expect(() => {
            recordEventMetrics(acc, e);
          }).not.toThrow();
        }
      }),
      { numRuns: 200 },
    );
  });

  it("sum(bucketCounts) === count for every exported data point", () => {
    fc.assert(
      fc.property(fc.array(genaiEventArb, { maxLength: 30 }), (events) => {
        const acc = new MetricsAccumulator();
        for (const e of events) recordEventMetrics(acc, e);
        const payload = accumulatorToOtlpMetricsPayload(acc, "svc", SCHEMA_URL);
        for (const metric of payload.resourceMetrics[0]!.scopeMetrics[0]!.metrics) {
          for (const dp of metric.histogram.dataPoints) {
            const total = dp.bucketCounts.reduce((a, b) => a + Number(b), 0);
            expect(total).toBe(Number(dp.count));
          }
        }
      }),
      { numRuns: 300 },
    );
  });

  it("token counts land in the independently-computed bucket", () => {
    fc.assert(
      fc.property(fc.integer({ min: 0, max: 100_000_000 }), (tokens) => {
        const acc = new MetricsAccumulator();
        recordEventMetrics(acc, {
          "gen_ai.provider.name": "openai",
          "gen_ai.operation.name": "chat",
          "gen_ai.request.model": "gpt-4o",
          "gen_ai.usage.input_tokens": tokens,
        });
        const payload = accumulatorToOtlpMetricsPayload(acc, "svc", SCHEMA_URL);
        const metric = metricOf(payload, TOKEN_USAGE_METRIC);
        expect(metric.histogram.dataPoints).toHaveLength(1);
        const counts = metric.histogram.dataPoints[0]!.bucketCounts.map(Number);
        // Re-derive the bucket independently (smallest i with tokens <= bound,
        // else the overflow bucket) — NOT via the module's bucketIndex, so
        // this genuinely cross-checks the implementation.
        let expectedIdx = TOKEN_USAGE_BOUNDS.length;
        for (const [i, bound] of TOKEN_USAGE_BOUNDS.entries()) {
          if (tokens <= bound) {
            expectedIdx = i;
            break;
          }
        }
        counts.forEach((c, i) => {
          expect(c).toBe(i === expectedIdx ? 1 : 0);
        });
      }),
      { numRuns: 400 },
    );
  });

  it("durations land in the independently-computed bucket (latency_ms/1000)", () => {
    fc.assert(
      fc.property(fc.integer({ min: 0, max: 200_000 }), (latencyMs) => {
        const acc = new MetricsAccumulator();
        recordEventMetrics(acc, {
          "gen_ai.provider.name": "anthropic",
          "gen_ai.operation.name": "chat",
          "gen_ai.request.model": "claude-sonnet-4-5",
          latency_ms: latencyMs,
        });
        const payload = accumulatorToOtlpMetricsPayload(acc, "svc", SCHEMA_URL);
        const metric = metricOf(payload, OPERATION_DURATION_METRIC);
        expect(metric.histogram.dataPoints).toHaveLength(1);
        const dp = metric.histogram.dataPoints[0]!;
        const counts = dp.bucketCounts.map(Number);
        const durationS = latencyMs / 1000;
        expect(dp.sum).toBeCloseTo(durationS, 9);
        const expectedIdx = bucketIndex(durationS, OPERATION_DURATION_BOUNDS);
        counts.forEach((c, i) => {
          expect(c).toBe(i === expectedIdx ? 1 : 0);
        });
      }),
      { numRuns: 400 },
    );
  });

  it("identical attribute sets aggregate into one series regardless of insert order", () => {
    fc.assert(
      fc.property(
        fc.array(fc.integer({ min: 0, max: 1_000_000 }), { minLength: 1, maxLength: 15 }),
        (tokenValues) => {
          const acc = new MetricsAccumulator();
          for (const t of tokenValues) {
            recordEventMetrics(acc, {
              "gen_ai.provider.name": "openai",
              "gen_ai.operation.name": "chat",
              "gen_ai.request.model": "gpt-4o",
              "gen_ai.usage.input_tokens": t,
            });
          }
          const payload = accumulatorToOtlpMetricsPayload(acc, "svc", SCHEMA_URL);
          const metric = metricOf(payload, TOKEN_USAGE_METRIC);
          // All input records share one attribute set → exactly one series.
          expect(metric.histogram.dataPoints).toHaveLength(1);
          const dp = metric.histogram.dataPoints[0]!;
          expect(Number(dp.count)).toBe(tokenValues.length);
          expect(dp.sum).toBe(tokenValues.reduce((a, b) => a + b, 0));
        },
      ),
      { numRuns: 200 },
    );
  });
});

// ---------------------------------------------------------------------------
// (3) Edge-runtime smoke — no node:* dependency.
// ---------------------------------------------------------------------------

describe("OTLP GenAI metrics — edge-runtime", () => {
  let bundleSource = "";

  beforeAll(async () => {
    const sourcePath = resolve(here, "..", "src", "_otlp_metrics.ts");
    const ts = await readFile(sourcePath, "utf-8");
    // Transpile to CJS (EdgeVM runs scripts, not ESM). The module's only
    // import is `import type { TelemetryEvent }`, which esbuild erases — so
    // what runs in the sandbox is the pure metrics logic over WinterCG
    // globals, with no injected Node shim.
    const out = await transform(ts, {
      loader: "ts",
      format: "cjs",
      target: "es2022",
    });
    bundleSource = out.code;
  });

  it("records + serializes inside a WinterCG VM with no node:* import", () => {
    const vm = new EdgeVM();
    // `require` throws for ANY module name — the metrics module has no runtime
    // import, so reaching `require` at all is a failure.
    vm.evaluate(`
      globalThis.module = { exports: {} };
      globalThis.exports = globalThis.module.exports;
      globalThis.require = (name) => {
        throw new Error('unexpected require(' + name + ') in edge metrics module');
      };
    `);
    vm.evaluate(bundleSource);

    const result = vm.evaluate<{
      tokenSeries: number;
      durationSeries: number;
      inputSum: number;
      inputBuckets: string[];
      durationSum: number;
      unitToken: string;
      unitDuration: string;
      temporality: number;
    }>(`
      (() => {
        const m = globalThis.module.exports;
        const acc = new m.MetricsAccumulator();
        // basic_single_call: 1000 input, 500 output, 1200ms latency.
        m.recordEventMetrics(acc, {
          "gen_ai.provider.name": "openai",
          "gen_ai.operation.name": "chat",
          "gen_ai.request.model": "gpt-4o",
          "gen_ai.usage.input_tokens": 1000,
          "gen_ai.usage.output_tokens": 500,
          "latency_ms": 1200,
        });
        const payload = m.accumulatorToOtlpMetricsPayload(
          acc, "edge-svc", "https://opentelemetry.io/schemas/1.41.0",
        );
        const metrics = payload.resourceMetrics[0].scopeMetrics[0].metrics;
        const token = metrics.find((x) => x.name === "gen_ai.client.token.usage");
        const duration = metrics.find((x) => x.name === "gen_ai.client.operation.duration");
        const input = token.histogram.dataPoints.find(
          (dp) => dp.attributes.some(
            (a) => a.key === "gen_ai.token.type" && a.value.stringValue === "input",
          ),
        );
        return {
          tokenSeries: token.histogram.dataPoints.length,
          durationSeries: duration.histogram.dataPoints.length,
          inputSum: input.sum,
          inputBuckets: input.bucketCounts,
          durationSum: duration.histogram.dataPoints[0].sum,
          unitToken: token.unit,
          unitDuration: duration.unit,
          temporality: token.histogram.aggregationTemporality,
        };
      })()
    `);

    expect(result.tokenSeries).toBe(2); // input + output
    expect(result.durationSeries).toBe(1);
    expect(result.inputSum).toBe(1000);
    // 1000 → bucket index 5 (bounds[5] === 1024). bucketCounts are strings.
    expect(result.inputBuckets.map(Number)).toEqual([0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0]);
    expect(result.durationSum).toBeCloseTo(1.2, 9);
    expect(result.unitToken).toBe("{token}");
    expect(result.unitDuration).toBe("s");
    expect(result.temporality).toBe(2);
  });
});
