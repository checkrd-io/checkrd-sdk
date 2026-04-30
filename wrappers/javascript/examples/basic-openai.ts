/**
 * Basic OpenAI instrumentation.
 *
 * Install:
 *   npm install checkrd openai
 *
 * Run:
 *   export OPENAI_API_KEY=sk-...
 *   export CHECKRD_API_KEY=ck_live_...   # optional
 *   npx tsx basic-openai.ts
 */
import { init, instrument, shutdown } from "checkrd";
import OpenAI from "openai";

async function main(): Promise<void> {
  // One-time global setup. Reads CHECKRD_API_KEY from the environment.
  init({ policy: "policy.yaml" });
  instrument();

  // Every `new OpenAI()` after `instrument()` is transparently routed
  // through the Checkrd policy engine.
  const client = new OpenAI({ apiKey: process.env["OPENAI_API_KEY"] });
  const response = await client.chat.completions.create({
    model: "gpt-4o",
    messages: [{ role: "user", content: "Hello in five words." }],
  });
  console.log(response.choices[0]?.message.content);

  await shutdown();
}

void main();
