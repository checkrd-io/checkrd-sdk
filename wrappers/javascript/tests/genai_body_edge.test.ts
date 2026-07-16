/**
 * Edge-runtime smoke test for the GenAI body extractor.
 *
 * `_genai_body.ts` runs inside the SDK's fetch hook, which executes on
 * Cloudflare Workers, Vercel Edge, Deno, and the browser. It must
 * therefore rely on nothing but WinterCG globals — `TextDecoder`,
 * `Headers`, `JSON`, `RegExp`, `Object` — and import no `node:*`
 * module, eagerly or lazily.
 *
 * Rather than assert that property structurally, we PROVE it: the
 * extractor source is transpiled and evaluated inside an
 * `@edge-runtime/vm` sandbox (the same WinterCG VM the published-bundle
 * edge test uses), whose `require` throws on any Node built-in. Then
 * we drive the new code paths — the `Headers`-instance lookup, the
 * `TextDecoder` byte path, the Anthropic normalization, and the
 * Bedrock header source — from inside the sandbox. A `node:*` touch or
 * a missing global would surface as a throw here.
 */
import { readFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { transform } from "esbuild";
import { EdgeVM } from "@edge-runtime/vm";
import { beforeAll, describe, expect, it } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const sourcePath = resolve(here, "..", "src", "_genai_body.ts");

let bundleSource = "";

beforeAll(async () => {
  const ts = await readFile(sourcePath, "utf-8");
  // Strip the type layer and emit CJS so the EdgeVM (which runs
  // scripts, not ESM modules) can link the `export function`s onto
  // `module.exports`. esbuild does NOT inject any Node shim for a
  // dependency-free module, so what runs in the VM is exactly the
  // extractor logic over WinterCG globals.
  const out = await transform(ts, {
    loader: "ts",
    format: "cjs",
    target: "es2022",
  });
  bundleSource = out.code;
});

describe("genai body extractor — edge-runtime", () => {
  it("evaluates with no node:* dependency and runs the new paths", () => {
    const vm = new EdgeVM();
    // `require` inside the sandbox throws for any module name — the
    // extractor has zero imports, so reaching it at all is a failure.
    vm.evaluate(`
      globalThis.module = { exports: {} };
      globalThis.exports = globalThis.module.exports;
      globalThis.require = (name) => {
        throw new Error('unexpected require(' + name + ') in edge extractor');
      };
    `);
    vm.evaluate(bundleSource);

    const result = vm.evaluate<Record<string, unknown>>(`
      (() => {
        const m = globalThis.module.exports;

        // (1) TextDecoder byte path — Anthropic response, normalized.
        const anthropicBody = new TextEncoder().encode(JSON.stringify({
          model: "claude-sonnet-4-5",
          usage: {
            input_tokens: 300,
            output_tokens: 350,
            cache_read_input_tokens: 1000,
            cache_creation_input_tokens: 200,
          },
        }));
        const anthropic = m.extractResponseAttrs("anthropic", anthropicBody);

        // (2) Headers-INSTANCE path — Bedrock token counts, mixed case.
        const h = new Headers({
          "X-Amzn-Bedrock-Input-Token-Count": "1200",
          "x-amzn-bedrock-output-token-count": "380",
        });
        const bedrock = m.extractResponseAttrs("aws.bedrock", undefined, h);

        // (3) plain-object header path, case-insensitive.
        const bedrockObj = m.extractResponseAttrs("aws.bedrock", undefined, {
          "X-AMZN-BEDROCK-INPUT-TOKEN-COUNT": "55",
        });

        // (4) request extraction from a JSON string.
        const req = m.extractRequestAttrs(
          "openai",
          JSON.stringify({ model: "gpt-4o", stream: true }),
        );

        return { anthropic, bedrock, bedrockObj, req };
      })()
    `);

    expect(result["anthropic"]).toEqual({
      "gen_ai.response.model": "claude-sonnet-4-5",
      "gen_ai.usage.input_tokens": 1500,
      "gen_ai.usage.output_tokens": 350,
      "gen_ai.usage.cache_read.input_tokens": 1000,
      "gen_ai.usage.cache_creation.input_tokens": 200,
    });
    expect(result["bedrock"]).toEqual({
      "gen_ai.usage.input_tokens": 1200,
      "gen_ai.usage.output_tokens": 380,
    });
    expect(result["bedrockObj"]).toEqual({
      "gen_ai.usage.input_tokens": 55,
    });
    expect(result["req"]).toEqual({
      "gen_ai.request.model": "gpt-4o",
      "gen_ai.request.stream": true,
    });
  });
});
