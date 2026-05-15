/**
 * Vercel AI SDK integration via middleware.
 *
 * Install:
 *   npm install checkrd ai @ai-sdk/openai
 *
 * Run:
 *   export OPENAI_API_KEY=sk-...
 *   export CHECKRD_API_KEY=ck_live_...
 *   export CHECKRD_AGENT_ID=...    # UUID from your dashboard
 *   npx tsx vercel-ai-sdk.ts
 *
 * The control plane is canonical: ``initAsync()`` fetches your
 * dashboard's published policy bundle and installs it before
 * returning. Same posture as OPA bundles / Envoy xDS.
 */
import { openai } from "@ai-sdk/openai";
import { generateText, wrapLanguageModel } from "ai";
import { initAsync, getEngine, getSink } from "checkrd";
import { checkrdMiddleware } from "checkrd/ai-sdk";

async function main(): Promise<void> {
  // initAsync() reads CHECKRD_API_KEY + CHECKRD_AGENT_ID from the
  // env, fetches the agent's published policy, and installs it
  // before resolving. ``getEngine()`` / ``getSink()`` then hand
  // back the same instances the rest of the SDK uses, so events
  // sent through the AI SDK middleware land on the same control-
  // plane sink as any other Checkrd-instrumented client.
  await initAsync();

  const model = wrapLanguageModel({
    model: openai("gpt-4o"),
    middleware: checkrdMiddleware({
      engine: getEngine(),
      enforce: true,
      agentId: process.env["CHECKRD_AGENT_ID"]!,
      sink: getSink(),
    }),
  });

  const { text } = await generateText({
    model,
    prompt: "Hello in five words.",
  });
  console.log(text);
}

void main();
