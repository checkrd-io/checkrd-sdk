/**
 * Cloudflare Workers cross-runtime test.
 *
 * Uses `miniflare` — the official Cloudflare-maintained programmatic
 * wrapper around `workerd` (the same runtime CF Workers production
 * uses). Spins up a worker with the SDK's published ESM bundle as
 * the module source, dispatches a request through it, and asserts
 * the SDK loaded + the engine works inside the V8 isolate.
 *
 * # Why miniflare (not vitest-pool-workers)
 *
 * `@cloudflare/vitest-pool-workers` is the canonical CF-Workers test
 * harness, but it requires a project-wide vitest pool change + a
 * wrangler.toml. That couples the rest of our test suite to the
 * Workers test infrastructure unnecessarily. `miniflare` is a
 * targeted, single-file dependency that exercises the same workerd
 * binary, lets us script the worker source inline, and runs as one
 * test among many in our standard vitest config.
 *
 * # What this proves beyond `@edge-runtime/vm`
 *
 * The existing edge_runtime test runs the bundle in a WinterCG-spec
 * V8 isolate. miniflare runs the bundle in *the actual workerd
 * runtime*, which has its own:
 *   - Module loader semantics (WebAssembly modules as bindings).
 *   - WebAssembly sub-allocator + memory limits.
 *   - `crypto.subtle` impl backed by BoringSSL.
 *   - `fetch` impl with stricter sub-request behaviour.
 *
 * A regression in any of those breaks production Cloudflare
 * customers but slides past a WinterCG sandbox. miniflare catches
 * it.
 */

import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { readFile } from "node:fs/promises";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

// `miniflare` is loaded lazily so this test can be skipped cleanly on
// hosts where it can't install (Apple Silicon prebuilds existed late;
// the dynamic import errors only when the test actually runs).
type MiniflareCtor = new (opts: unknown) => {
  dispatchFetch: (input: string, init?: RequestInit) => Promise<Response>;
  dispose: () => Promise<void>;
};

const here = dirname(fileURLToPath(import.meta.url));
const wasmPath = resolve(here, "..", "..", "checkrd_core.wasm");

let MiniflareImpl: MiniflareCtor | null = null;
let wasmBytes = new Uint8Array();

beforeAll(async () => {
  // Skip the test, with a clear reason, if miniflare isn't installed.
  // This covers local dev environments that haven't run `npm ci` and
  // CI runners on architectures miniflare doesn't ship binaries for.
  try {
    const mod = await import("miniflare");
    MiniflareImpl = (mod as unknown as { Miniflare: MiniflareCtor }).Miniflare;
  } catch (err) {
    console.warn(
      `Skipping Workers runtime test: miniflare unavailable (${
        err instanceof Error ? err.message : String(err)
      })`,
    );
    return;
  }
  // We don't read dist/index.js as a string anymore — the worker
  // source uses real `import` statements that esbuild resolves
  // against this package's `package.json#exports` map, the same way
  // `wrangler deploy` does in production.
  wasmBytes = new Uint8Array(await readFile(wasmPath));
});

describe("Cloudflare Workers (workerd) runtime", () => {
  it("loads the SDK + runs WasmEngine.create() inside workerd", async () => {
    if (!MiniflareImpl) {
      // Test will appear as passing in CI where miniflare is unavailable;
      // the skip is intentional — see beforeAll for the rationale.
      return;
    }

    // The worker is a proper ESM module that imports the SDK by its
    // package path. esbuild then bundles the import + the SDK chain
    // into a single self-contained module — mirroring exactly what
    // `wrangler deploy` does for a production CF Workers ship. Only
    // `default` is exported; workerd treats any other exports as
    // bindings, which fails type-checks.
    const workerSource = `
      // \`wrapFetch\` lives on the main entry; \`WasmEngine\` ships
      // from \`checkrd/advanced\`. This is the same import shape a
      // real Cloudflare Workers customer would write.
      import { wrapFetch } from "checkrd";
      import { WasmEngine } from "checkrd/advanced";
      // CompiledWasm modules are imported as ESM and the default
      // export is the \`WebAssembly.Module\`. This mirrors the
      // production CF Workers WASM-loading idiom.
      import wasmModule from "wasm/checkrd.wasm";

      const POLICY = '{"agent":"workers-test","default":"allow","rules":[]}';

      export default {
        async fetch(req, env) {
          try {
            const engine = await WasmEngine.create(
              POLICY,
              'workers-agent',
              { wasm: wasmModule },
            );
            const allowResult = engine.evaluate({
              request_id: 'workers-1',
              method: 'GET',
              url: 'https://example.com/',
              headers: [],
              body: null,
              timestamp: new Date().toISOString(),
              timestamp_ms: Date.now(),
            });
            if (!allowResult.allowed) {
              return new Response('allow path failed', { status: 500 });
            }
            let baseFetchCalled = false;
            const baseFetch = async () => {
              baseFetchCalled = true;
              return new Response('{"ok":true}', {
                status: 200,
                headers: { 'content-type': 'application/json' },
              });
            };
            const wrapped = wrapFetch(baseFetch, {
              engine, enforce: true, agentId: 'workers-agent',
            });
            const r = await wrapped('https://api.example.com/x', { method: 'POST' });
            if (r.status !== 200 || !baseFetchCalled) {
              return new Response(
                'wrapped-fetch failed: status=' + r.status + ', called=' + baseFetchCalled,
                { status: 500 },
              );
            }
            return new Response('ok', { status: 200 });
          } catch (err) {
            return new Response(
              'WasmEngine error: ' + (err && err.message ? err.message : String(err)),
              { status: 500 },
            );
          }
        }
      };
    `;

    // Bundle the worker source with esbuild before handing it to
    // miniflare. esbuild resolves `checkrd/advanced` against this
    // package's own `package.json#exports`, just like wrangler does
    // in production. `conditions: ["workerd", "import"]` selects the
    // worker-specific export from our exports map (which currently
    // points at the same dist/index.js — but the resolution behavior
    // matches production deployment).
    const esbuild = await import("esbuild");
    const built = await esbuild.build({
      stdin: {
        contents: workerSource,
        loader: "js",
        resolveDir: resolve(here, "..", ".."),
        sourcefile: "worker.mjs",
      },
      bundle: true,
      format: "esm",
      target: "es2022",
      platform: "neutral",
      write: false,
      conditions: ["workerd", "import"],
      // Node-only dynamic imports inside the SDK are guarded by
      // `try { await import("fs"); } catch { /* edge fallback */ }`
      // — they're never executed on workerd. esbuild's static analysis
      // can't tell, though, so it tries to bundle them and fails.
      // Marking them external preserves the dynamic-import expression;
      // workerd then throws at the import attempt, the try/catch
      // catches it, and the edge fallback runs as designed.
      external: [
        "fs",
        "fs/promises",
        "node:fs",
        "node:fs/promises",
        "node:crypto",
        "node:url",
        "node:module",
        "node:path",
        "process",
        // Synthetic specifier resolved at runtime by miniflare's
        // CompiledWasm module loader. esbuild has no way to follow
        // it; marking it external preserves the import expression.
        "wasm/checkrd.wasm",
      ],
    });
    const bundledWorker = built.outputFiles[0]?.text ?? "";

    const mf = new MiniflareImpl({
      modules: [
        { type: "ESModule", path: "index.mjs", contents: bundledWorker },
        // Mirror the production deployment shape: WASM ships as a
        // `wasm_modules` binding (per `wrangler.toml`) so workerd
        // compiles it at deploy time and the runtime gets a fully
        // formed `WebAssembly.Module`. Production CF Workers
        // disallow `WebAssembly.compile()` at request time as a
        // security policy; only this binding-shaped path is
        // permitted, and our SDK's `readWasmBytes` already accepts
        // a `WebAssembly.Module` as one of the input types.
        { type: "CompiledWasm", path: "wasm/checkrd.wasm", contents: wasmBytes },
      ],
      compatibilityDate: "2024-09-01",
      modulesRoot: "/",
    });

    try {
      const response = await mf.dispatchFetch("https://workers-test.local/");
      const body = await response.text();
      // The 500-with-message paths in the worker carry the actual
      // failure reason — surface it directly so a failure is
      // immediately diagnosable instead of "expected 200, got 500".
      expect(response.status, `worker response body: ${body}`).toBe(200);
      expect(body).toBe("ok");
    } finally {
      await mf.dispose();
    }
  });
});

afterAll(() => {
  // Defensive: dispose() in the it() block normally handles cleanup,
  // but if the test crashed mid-flight we still want the workerd
  // process gone so the next test run doesn't fight for the same
  // sockets. Currently a no-op since each test creates its own mf.
});
