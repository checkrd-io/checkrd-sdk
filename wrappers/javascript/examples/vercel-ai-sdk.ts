/**
 * Vercel AI SDK integration via middleware.
 *
 * Install:
 *   npm install checkrd ai @ai-sdk/openai
 *
 * Run:
 *   export OPENAI_API_KEY=sk-...
 *   npx tsx vercel-ai-sdk.ts
 */
import { openai } from "@ai-sdk/openai";
import { generateText, wrapLanguageModel } from "ai";
import { init, WasmEngine, loadConfig } from "checkrd";
import { checkrdMiddleware } from "checkrd/ai-sdk";

async function main(): Promise<void> {
  // init() wires up the global context; we also grab the engine for
  // the middleware below (which runs outside the usual fetch path).
  init({ policy: "policy.yaml" });
  const engine = new WasmEngine(loadConfig("policy.yaml"), "example-agent");

  const model = wrapLanguageModel({
    model: openai("gpt-4o"),
    middleware: checkrdMiddleware({
      engine,
      enforce: true,
      agentId: "example-agent",
    }),
  });

  const { text } = await generateText({
    model,
    prompt: "Hello in five words.",
  });
  console.log(text);
}

void main();
