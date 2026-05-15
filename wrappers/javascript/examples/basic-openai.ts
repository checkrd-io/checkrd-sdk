/**
 * Basic OpenAI instrumentation.
 *
 * Install:
 *   npm install checkrd openai
 *
 * Run:
 *   export OPENAI_API_KEY=sk-...
 *   export CHECKRD_API_KEY=ck_live_...
 *   export CHECKRD_AGENT_ID=...    # UUID from your dashboard
 *   npx tsx basic-openai.ts
 *
 * The control plane is canonical: ``init()`` fetches your
 * dashboard's published policy bundle and installs it before
 * vendor SDKs ship their first byte. No ``policy:`` argument in
 * app code — the dashboard is the source of truth.
 */
import { initAsync, instrument, shutdown } from "checkrd";
import OpenAI from "openai";

async function main(): Promise<void> {
  // One-time global setup. Reads CHECKRD_API_KEY +
  // CHECKRD_AGENT_ID from the environment, fetches the published
  // policy bundle, and installs it before returning. ``initAsync``
  // works in every runtime — Node, Bun, Deno, Cloudflare Workers,
  // Vercel Edge — without ``node:fs`` imports.
  await initAsync();
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
