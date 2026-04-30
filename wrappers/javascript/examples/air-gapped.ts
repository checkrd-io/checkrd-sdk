/**
 * Tier 3 air-gapped deployment.
 *
 * Policy evaluation happens entirely in-process. Telemetry is written
 * to a local JSON Lines file instead of shipped to any cloud service.
 * Suitable for environments where outbound traffic to api.checkrd.io
 * is blocked — regulated enterprises, classified deployments, on-prem
 * labs.
 *
 * Install:
 *   npm install checkrd openai
 *
 * Run:
 *   export OPENAI_API_KEY=sk-...
 *   npx tsx air-gapped.ts
 */
import { init, instrument, JsonFileSink, shutdown } from "checkrd";
import OpenAI from "openai";

async function main(): Promise<void> {
  const logPath = "/tmp/checkrd-events.jsonl";

  // No `apiKey` → SDK runs without talking to the control plane. The
  // JsonFileSink appends newline-delimited JSON, readable by Vector /
  // Fluent Bit / Promtail / any log shipper.
  init({
    policy: "policy.yaml",
    sink: new JsonFileSink({ path: logPath }),
  });
  instrument();

  const client = new OpenAI({ apiKey: process.env["OPENAI_API_KEY"] });
  await client.chat.completions.create({
    model: "gpt-4o",
    messages: [{ role: "user", content: "Hello in five words." }],
  });

  await shutdown();
  console.log(`Events written to ${logPath}`);
}

void main();
