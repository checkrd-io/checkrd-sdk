/**
 * Edge-runtime smoke test for the streaming usage tap (M-11).
 *
 * `_stream_capture.ts` runs inside the SDK's fetch hook, which executes
 * on Cloudflare Workers, Vercel Edge, Deno, and the browser. The pure
 * frame-driven tap (`captureUsageFromFrames`) must therefore rely on
 * nothing but WinterCG globals — `JSON`, `String`, `Object`, `Number` —
 * and import no `node:*` module, eagerly or lazily.
 *
 * Rather than assert that property structurally, we PROVE it: the tap
 * source is transpiled and evaluated inside an `@edge-runtime/vm`
 * sandbox (the same WinterCG VM the body extractor's edge test uses),
 * whose `require` throws on any Node built-in. Then we drive the new
 * code paths — the Anthropic inclusive-input normalization, the
 * OpenAI cache/reasoning detail counters, and the untallied-on-
 * abandonment branch — from inside the sandbox. A `node:*` touch or a
 * missing global would surface as a throw here.
 *
 * Note: `_stream_capture.ts` references the `Response` / `ReadableStream`
 * / `TextDecoder` globals in its live `teeResponseForTokens` /
 * `captureStreamTokens` path. Those are all WinterCG globals too (and
 * present in EdgeVM), so the whole module links cleanly; we exercise the
 * pure seam, which is the part the fixtures pin.
 */
import { readFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { transform } from "esbuild";
import { EdgeVM } from "@edge-runtime/vm";
import { beforeAll, describe, expect, it } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const sourcePath = resolve(here, "..", "src", "_stream_capture.ts");

let bundleSource = "";

beforeAll(async () => {
  const ts = await readFile(sourcePath, "utf-8");
  // Strip the type layer and emit CJS so the EdgeVM (which runs
  // scripts, not ESM modules) can link the `export function`s onto
  // `module.exports`. esbuild injects no Node shim for this
  // dependency-free module, so what runs in the VM is exactly the tap
  // logic over WinterCG globals. We additionally neutralize the
  // type-only `import type` lines (esbuild already elides them).
  const out = await transform(ts, {
    loader: "ts",
    format: "cjs",
    target: "es2022",
  });
  bundleSource = out.code;
});

describe("streaming usage tap — edge-runtime", () => {
  it("evaluates with no node:* dependency and runs the new paths", () => {
    const vm = new EdgeVM();
    // `require` inside the sandbox throws for any module name — the tap
    // imports only `import type` (elided), so reaching `require` at all
    // is a failure.
    vm.evaluate(`
      globalThis.module = { exports: {} };
      globalThis.exports = globalThis.module.exports;
      globalThis.require = (name) => {
        throw new Error('unexpected require(' + name + ') in edge stream tap');
      };
    `);
    vm.evaluate(bundleSource);

    const result = vm.evaluate<Record<string, unknown>>(`
      (() => {
        const m = globalThis.module.exports;

        // (1) OpenAI: include_usage terminal frame with cache + reasoning
        //     detail counters (native inclusive, no normalization).
        const openai = m.captureUsageFromFrames("openai", [
          'data: ' + JSON.stringify({ choices: [{ delta: { content: "hi" } }] }) + '\\n\\n',
          'data: ' + JSON.stringify({
            choices: [],
            usage: {
              prompt_tokens: 1000,
              completion_tokens: 500,
              prompt_tokens_details: { cached_tokens: 800 },
              completion_tokens_details: { reasoning_tokens: 200 },
            },
          }) + '\\n\\n',
          'data: [DONE]\\n\\n',
        ], true);

        // (2) Anthropic: message_start (cache-exclusive input) +
        //     terminal message_delta. Inclusive normalization:
        //     300 + 1000 + 200 = 1500.
        const anthropic = m.captureUsageFromFrames("anthropic", [
          'event: message_start\\ndata: ' + JSON.stringify({
            type: "message_start",
            message: { usage: {
              input_tokens: 300, output_tokens: 1,
              cache_read_input_tokens: 1000, cache_creation_input_tokens: 200,
            } },
          }) + '\\n\\n',
          'event: message_delta\\ndata: ' + JSON.stringify({
            type: "message_delta",
            delta: { stop_reason: "end_turn" },
            usage: { output_tokens: 350 },
          }) + '\\n\\n',
          'event: message_stop\\ndata: ' + JSON.stringify({ type: "message_stop" }) + '\\n\\n',
        ], true);

        // (3) Abandonment: OpenAI stream cut before the include_usage
        //     frame => untallied + empty usage (engine never estimates).
        const abandoned = m.captureUsageFromFrames("openai", [
          'data: ' + JSON.stringify({ choices: [{ delta: { content: "par" } }] }) + '\\n\\n',
        ], false);

        // (4) Malformed frames must not throw inside the sandbox.
        let threw = false;
        try {
          m.captureUsageFromFrames("anthropic", ["not json", "data: {oops", ""], true);
        } catch (e) {
          threw = true;
        }

        return { openai, anthropic, abandoned, threw };
      })()
    `);

    expect(result["openai"]).toEqual({
      usageAttrs: {
        "gen_ai.usage.input_tokens": 1000,
        "gen_ai.usage.output_tokens": 500,
        "gen_ai.usage.cache_read.input_tokens": 800,
        "gen_ai.usage.reasoning.output_tokens": 200,
      },
    });
    expect(result["anthropic"]).toEqual({
      usageAttrs: {
        "gen_ai.usage.input_tokens": 1500,
        "gen_ai.usage.output_tokens": 350,
        "gen_ai.usage.cache_read.input_tokens": 1000,
        "gen_ai.usage.cache_creation.input_tokens": 200,
      },
    });
    expect(result["abandoned"]).toEqual({
      usageAttrs: {},
      pricingStatus: "untallied",
    });
    expect(result["threw"]).toBe(false);
  });
});
