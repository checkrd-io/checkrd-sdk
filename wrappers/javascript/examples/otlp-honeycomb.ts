/**
 * Dual-export telemetry: Checkrd control plane + Honeycomb via OTLP.
 *
 * The OtlpSink ships every policy decision and request event as an
 * OTel span. Any OTLP/HTTP-JSON collector works — Honeycomb, Grafana
 * Cloud, Datadog (with the right auth headers), Axiom, New Relic, or
 * your own OpenTelemetry Collector.
 *
 * Install:
 *   npm install checkrd openai
 *
 * Run:
 *   export OPENAI_API_KEY=sk-...
 *   export CHECKRD_API_KEY=ck_live_...
 *   export HONEYCOMB_API_KEY=...
 *   npx tsx otlp-honeycomb.ts
 */
import { CompositeSink, init, instrument, OtlpSink, shutdown } from "checkrd";
import OpenAI from "openai";

async function main(): Promise<void> {
  const otlp = new OtlpSink({
    endpoint: "https://api.honeycomb.io",
    headers: {
      "x-honeycomb-team": process.env["HONEYCOMB_API_KEY"] ?? "",
      "x-honeycomb-dataset": "checkrd",
    },
    serviceName: "checkrd-example",
  });

  // `sink` overrides the default ControlPlaneSink. Use CompositeSink to
  // emit to multiple destinations at once.
  init({
    policy: "policy.yaml",
    apiKey: process.env["CHECKRD_API_KEY"],
    sink: new CompositeSink([otlp]),
  });
  instrument();

  const client = new OpenAI({ apiKey: process.env["OPENAI_API_KEY"] });
  await client.chat.completions.create({
    model: "gpt-4o",
    messages: [{ role: "user", content: "Hello in five words." }],
  });

  // OtlpSink flushes pending spans on close.
  await shutdown();
}

void main();
