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
 * Two layers of validation run here:
 *
 *   1. **Source** — imports from the ``src/`` paths (mirrors what tsup
 *      writes to dist) and asserts each promised symbol is reachable.
 *      Runs without a prior ``npm run build``.
 *   2. **Packaged export map** — resolves every ``package.json#exports``
 *      target for each subpath and (a) always maps it back to a source
 *      file to catch a mistyped path even before a build, and (b) when
 *      ``dist/`` is present, asserts the built ``import``/``require``/
 *      ``types`` files actually exist AND that the ESM build exports the
 *      promised symbols. Before this second layer, a wrong file path in
 *      ``package.json#exports`` (e.g. ``_openai.js`` renamed but the map
 *      not updated) would sail through — the source import still
 *      resolved. Now it fails here at build time, not in a customer's
 *      ``module-not-found``.
 */

import { existsSync } from "node:fs";
import { dirname, resolve as resolvePath } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import { describe, expect, it } from "vitest";

/** Absolute path to the wrapper package root (parent of ``tests/``). */
const PKG_ROOT = resolvePath(dirname(fileURLToPath(import.meta.url)), "..");

// Vendor instrumentors — one class per vendor SDK.
import * as openaiSubpath from "../src/integrations/_openai.js";
import * as anthropicSubpath from "../src/integrations/_anthropic.js";
import * as cohereSubpath from "../src/integrations/_cohere.js";
import * as groqSubpath from "../src/integrations/_groq.js";
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
    const exportsMap = await loadExportsMap();
    for (const { subpath } of CONTRACTS) {
      const key = subpath.replace(/^checkrd/, ".");
      expect(
        exportsMap[key],
        `${subpath} must have a "${key}" entry in package.json#exports`,
      ).toBeDefined();
    }
  });
});

// ---------------------------------------------------------------------------
// Packaged export-map validation — the part the source-import checks above
// cannot see. A wrong ``dist`` path in ``package.json#exports`` would leave
// every source-based test green while breaking the actual published package.
// ---------------------------------------------------------------------------

/** Recursively collect every string leaf (target file) under an exports node. */
function collectTargets(node: unknown, out: string[] = []): string[] {
  if (typeof node === "string") {
    out.push(node);
  } else if (node !== null && typeof node === "object") {
    for (const value of Object.values(node)) collectTargets(value, out);
  }
  return out;
}

/**
 * Map a ``package.json#exports`` target back to the ``src`` file tsup
 * builds it from. Every output flavour (``.js`` ESM, ``.cjs`` CJS,
 * ``.d.ts`` / ``.d.cts`` types) is generated from the same ``.ts``
 * source, so a mistyped target resolves to a non-existent source file
 * — which this test then flags, even before a build has run.
 */
function targetToSource(target: string): string {
  const rel = target.replace(/^\.\/dist\//, "");
  const stem = rel.replace(/\.(d\.cts|d\.ts|cjs|mjs|js)$/, "");
  return resolvePath(PKG_ROOT, "src", `${stem}.ts`);
}

/** Resolve a ``./dist/...`` target to an absolute filesystem path. */
function targetToDist(target: string): string {
  return resolvePath(PKG_ROOT, target.replace(/^\.\//, ""));
}

async function loadExportsMap(): Promise<Record<string, unknown>> {
  const pkg = (await import("../package.json", {
    with: { type: "json" },
  })) as unknown as { default: { exports: Record<string, unknown> } };
  return pkg.default.exports;
}

/** Whether a ``dist`` build exists — gates the artifact-resolution checks. */
const DIST_BUILT = existsSync(resolvePath(PKG_ROOT, "dist", "index.js"));

describe("packaged export map — every target maps to a real source", () => {
  it.each(CONTRACTS)(
    "$subpath: every exports target resolves to an existing src file",
    async ({ subpath }: SubpathContract) => {
      const exportsMap = await loadExportsMap();
      const key = subpath.replace(/^checkrd/, ".");
      const entry = exportsMap[key];
      expect(entry, `${subpath} missing from package.json#exports`).toBeDefined();
      const targets = collectTargets(entry);
      // At minimum: import.default + require.default + their two types.
      expect(targets.length).toBeGreaterThanOrEqual(2);
      for (const target of targets) {
        const src = targetToSource(target);
        expect(
          existsSync(src),
          `${subpath}: exports target "${target}" maps to missing source "${src}"`,
        ).toBe(true);
      }
    },
  );
});

describe.skipIf(!DIST_BUILT)(
  "packaged export map — built dist artifacts resolve + export symbols",
  () => {
    it.each(CONTRACTS)(
      "$subpath: every declared target file exists in dist",
      async ({ subpath }: SubpathContract) => {
        const exportsMap = await loadExportsMap();
        const key = subpath.replace(/^checkrd/, ".");
        const targets = collectTargets(exportsMap[key]);
        for (const target of targets) {
          const dist = targetToDist(target);
          expect(
            existsSync(dist),
            `${subpath}: exports target "${target}" does not exist in dist — ` +
              "the package.json#exports path is wrong or the entry is not built",
          ).toBe(true);
        }
      },
    );

    it.each(CONTRACTS)(
      "$subpath: the packaged ESM entry exports its primary symbols",
      async ({ subpath, primaryExports }: SubpathContract) => {
        const exportsMap = await loadExportsMap();
        const key = subpath.replace(/^checkrd/, ".");
        const entry = exportsMap[key] as { import?: { default?: string } };
        const esm = entry.import?.default;
        expect(esm, `${subpath} has no import.default target`).toBeDefined();
        // Import the ACTUAL built artifact the package ships (via a
        // file:// URL so vitest loads the real ESM, not the source),
        // then assert the promised symbols are reachable on it. This is
        // the resolution path a customer's bundler follows.
        const mod = (await import(
          pathToFileURL(targetToDist(esm!)).href
        )) as Record<string, unknown>;
        for (const name of primaryExports) {
          expect(
            mod[name],
            `${subpath} built ESM (${esm ?? "?"}) must export "${name}"`,
          ).toBeDefined();
        }
      },
    );
  },
);
