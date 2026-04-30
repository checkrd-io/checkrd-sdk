import { defineConfig } from "tsup";

/**
 * Dual-publish (ESM + CJS) with `.d.ts` generated straight from source.
 *
 * Each integration is built as its own entry point so consumers who
 * import only `checkrd/openai` (or `checkrd/next`, etc.) bundle just
 * that one — `treeshake: true` plus `sideEffects: false` in
 * `package.json` ensures the umbrella `index` entry never drags
 * unrelated integrations into the user's build. Subpath exports live
 * in `package.json#exports`; the entry-point list here must stay in
 * lockstep with that export map.
 *
 * `shims: false` is the deliberate setting. tsup's "shims" feature
 * injects a `__filename` / `__dirname` polyfill for ESM bundles,
 * which pulls `import 'path'; import 'url';` to the top of every
 * output file. Those are eager Node-only imports that workerd /
 * Cloudflare Workers correctly reject — turning the bundle into a
 * load-time crash on edge runtimes. We never reference `__filename`
 * or `__dirname` in source (we use `import.meta.url`), so the shim
 * is dead weight whose only effect is to break edge.
 *
 * If a future contributor reaches for `__filename`, the build will
 * fail loudly under ESM (no shim) — re-add the shim selectively
 * for the CJS format only and add a worker-runtime test that proves
 * the change didn't reintroduce the eager Node imports.
 *
 * # Source maps
 *
 * Off by default. Stripe Node and OpenAI Node both ship without source
 * maps because, for a security-critical SDK, the maps reveal:
 *   - internal-only modules and their relative paths (the file tree
 *     leaks even when names are minified),
 *   - comments and dead code that tree-shaking would otherwise hide,
 *   - implementation details an attacker reverse-engineering the
 *     telemetry signing path would otherwise have to recover.
 *
 * Contributors who need maps for local debugging can opt in via
 * `CHECKRD_SOURCEMAP=1`. The npm publish workflow MUST run with that
 * variable unset; the size budgets in `package.json#size-limit` only
 * make sense against the un-mapped artifacts.
 */
const SOURCEMAPS_ENABLED = process.env["CHECKRD_SOURCEMAP"] === "1";

export default defineConfig({
  entry: {
    index: "src/index.ts",
    // Power-user surface — engine internals, sinks, identity providers,
    // watchers, retry primitives. See src/advanced.ts for the rationale.
    advanced: "src/advanced.ts",
    // Vendor integrations
    "integrations/_openai": "src/integrations/_openai.ts",
    "integrations/_anthropic": "src/integrations/_anthropic.ts",
    "integrations/_cohere": "src/integrations/_cohere.ts",
    "integrations/_groq": "src/integrations/_groq.ts",
    "integrations/_mistral": "src/integrations/_mistral.ts",
    "integrations/_together": "src/integrations/_together.ts",
    "integrations/_google_genai": "src/integrations/_google_genai.ts",
    // Framework adapters
    "integrations/_ai_sdk": "src/integrations/_ai_sdk.ts",
    "integrations/_next": "src/integrations/_next.ts",
    "integrations/_cloudflare": "src/integrations/_cloudflare.ts",
    "integrations/_hono": "src/integrations/_hono.ts",
    "integrations/_mastra": "src/integrations/_mastra.ts",
    "integrations/_mcp": "src/integrations/_mcp.ts",
    "integrations/_langchain": "src/integrations/_langchain.ts",
    "integrations/_openai_agents": "src/integrations/_openai_agents.ts",
    "integrations/_claude_agent_sdk": "src/integrations/_claude_agent_sdk.ts",
  },
  format: ["esm", "cjs"],
  outExtension: ({ format }) => ({ js: format === "cjs" ? ".cjs" : ".js" }),
  dts: true,
  sourcemap: SOURCEMAPS_ENABLED,
  clean: true,
  // See header comment — shims would inject Node-only imports that
  // workerd/Cloudflare-Workers reject at module load. Off because no
  // source file references `__filename` / `__dirname`.
  shims: false,
  target: "node18",
  splitting: false,
  treeshake: true,
});
