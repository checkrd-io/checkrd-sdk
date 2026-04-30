/**
 * Subpath export contract tests.
 *
 * Each entry in ``package.json#exports`` advertises a public subpath
 * (``checkrd/openai``, ``checkrd/anthropic``, ``checkrd/advanced``,
 * etc.). Users rely on those subpaths to import only what they need
 * — but the package.json declaration alone doesn't prove the source
 * file actually exports the symbols its name implies. This test pins
 * the contract: every advertised subpath imports cleanly AND exposes
 * its primary symbol. A regression here surfaces at build time
 * instead of as "module-not-found" in a customer's app.
 *
 * Complementary to:
 *
 *   - ``scripts/install-sanity.mjs`` — exercises the same contract
 *     post-``npm pack`` against the published-tarball-shaped install.
 *     Catches package.json#exports map errors (wrong file extension,
 *     missing condition).
 *   - ``npm run attw`` — verifies the ``types`` condition resolves
 *     correctly for every subpath under node16 / bundler / node10.
 *
 * This file imports from the SOURCE paths (mirrors what tsup writes
 * to dist and what package.json maps the subpaths to). That keeps
 * the test runnable without a prior ``npm run build``.
 */

import { describe, expect, it } from "vitest";

// Vendor instrumentors — one class per vendor SDK.
import * as openaiSubpath from "../src/integrations/_openai.js";
import * as anthropicSubpath from "../src/integrations/_anthropic.js";
import * as cohereSubpath from "../src/integrations/_cohere.js";
import * as groqSubpath from "../src/integrations/_groq.js";
import * as mistralSubpath from "../src/integrations/_mistral.js";
import * as togetherSubpath from "../src/integrations/_together.js";
import * as googleGenAISubpath from "../src/integrations/_google_genai.js";

// Framework / protocol adapters.
import * as aiSdkSubpath from "../src/integrations/_ai_sdk.js";
import * as nextSubpath from "../src/integrations/_next.js";
import * as cloudflareSubpath from "../src/integrations/_cloudflare.js";
import * as honoSubpath from "../src/integrations/_hono.js";
import * as mastraSubpath from "../src/integrations/_mastra.js";
import * as mcpSubpath from "../src/integrations/_mcp.js";
import * as langchainSubpath from "../src/integrations/_langchain.js";
import * as openaiAgentsSubpath from "../src/integrations/_openai_agents.js";
import * as claudeAgentSdkSubpath from "../src/integrations/_claude_agent_sdk.js";

// Power-user surface.
import * as advancedSubpath from "../src/advanced.js";

interface SubpathContract {
  /** Subpath as advertised in ``package.json#exports``. */
  subpath: string;
  /** Imported namespace object. */
  module: Record<string, unknown>;
  /** Primary named exports the subpath promises. */
  primaryExports: readonly string[];
}

const CONTRACTS: readonly SubpathContract[] = [
  {
    subpath: "checkrd/openai",
    module: openaiSubpath,
    primaryExports: ["OpenAIInstrumentor"],
  },
  {
    subpath: "checkrd/anthropic",
    module: anthropicSubpath,
    primaryExports: ["AnthropicInstrumentor"],
  },
  {
    subpath: "checkrd/cohere",
    module: cohereSubpath,
    primaryExports: ["CohereInstrumentor"],
  },
  {
    subpath: "checkrd/groq",
    module: groqSubpath,
    primaryExports: ["GroqInstrumentor"],
  },
  {
    subpath: "checkrd/mistral",
    module: mistralSubpath,
    primaryExports: ["MistralInstrumentor"],
  },
  {
    subpath: "checkrd/together",
    module: togetherSubpath,
    primaryExports: ["TogetherInstrumentor"],
  },
  {
    subpath: "checkrd/google-genai",
    module: googleGenAISubpath,
    primaryExports: ["GoogleGenAIInstrumentor"],
  },
  {
    subpath: "checkrd/ai-sdk",
    module: aiSdkSubpath,
    primaryExports: ["checkrdMiddleware"],
  },
  {
    subpath: "checkrd/next",
    module: nextSubpath,
    primaryExports: ["initCheckrd", "checkrdRoute", "checkrdAction"],
  },
  {
    subpath: "checkrd/cloudflare",
    module: cloudflareSubpath,
    primaryExports: ["withCheckrd"],
  },
  {
    subpath: "checkrd/hono",
    module: honoSubpath,
    primaryExports: ["checkrdHono"],
  },
  {
    subpath: "checkrd/mastra",
    module: mastraSubpath,
    primaryExports: ["wrapMastraAgent", "checkrdMastraTelemetry"],
  },
  {
    subpath: "checkrd/mcp",
    module: mcpSubpath,
    primaryExports: ["wrapMcpClient", "wrapMcpServer"],
  },
  {
    subpath: "checkrd/langchain",
    module: langchainSubpath,
    primaryExports: ["CheckrdCallbackHandler"],
  },
  {
    subpath: "checkrd/openai-agents",
    module: openaiAgentsSubpath,
    primaryExports: ["CheckrdTracingProcessor"],
  },
  {
    subpath: "checkrd/claude-agent-sdk",
    module: claudeAgentSdkSubpath,
    primaryExports: [
      "makePreToolUseHook",
      "makePostToolUseHook",
      "makeUserPromptSubmitHook",
    ],
  },
  {
    subpath: "checkrd/advanced",
    module: advancedSubpath,
    primaryExports: [
      "WasmEngine",
      "TelemetryBatcher",
      "ControlReceiver",
      "loadConfig",
      "CircuitBreaker",
    ],
  },
];

describe("subpath export contract", () => {
  it.each(CONTRACTS)(
    "$subpath imports cleanly",
    ({ module }: SubpathContract) => {
      // Module loaded with at least one named export — catches
      // accidental empty-file regressions.
      expect(Object.keys(module).length).toBeGreaterThan(0);
    },
  );

  it.each(CONTRACTS)(
    "$subpath exposes its primary exports",
    ({ subpath, module, primaryExports }: SubpathContract) => {
      // Every promised symbol is non-undefined. We don't assert on
      // type (function vs class vs const) because some adapters expose
      // factories and others classes — the only contract is
      // "name is reachable".
      for (const name of primaryExports) {
        expect(
          module[name],
          `${subpath} must export "${name}"`,
        ).toBeDefined();
      }
    },
  );
});

describe("subpath ↔ package.json#exports parity", () => {
  it("every subpath under test corresponds to a package.json export", async () => {
    // Programmatic check: read package.json and confirm each
    // contract's subpath has a matching ``./<x>`` exports entry.
    // Catches the "added a subpath in code but forgot package.json"
    // regression, which install-sanity would catch only post-pack.
    const pkg = (await import("../package.json", {
      with: { type: "json" },
    })) as unknown as { default: { exports: Record<string, unknown> } };
    const exportsMap = pkg.default.exports;
    for (const { subpath } of CONTRACTS) {
      const key = subpath.replace(/^checkrd/, ".");
      expect(
        exportsMap[key],
        `${subpath} must have a "${key}" entry in package.json#exports`,
      ).toBeDefined();
    }
  });
});
