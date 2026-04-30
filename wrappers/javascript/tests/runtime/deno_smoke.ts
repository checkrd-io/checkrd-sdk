/**
 * Deno cross-runtime smoke test.
 *
 * Run with: `deno test --allow-read=. --allow-net=none tests/runtime/deno_smoke.ts`
 *
 * # What this proves
 *
 * 1. The published ESM bundle (`dist/index.js`) imports cleanly in
 *    Deno without `node:fs` / `node:crypto` / `node:url` shim flags.
 * 2. `WasmEngine.create()` works against the real Deno WebAssembly
 *    + Web Crypto implementations (no V8-Isolate-only assumptions).
 * 3. A wrapped `fetch` correctly intercepts a request, runs the
 *    policy engine, and either passes through (allow) or denies
 *    (deny). This is the production code path on a Deno Deploy
 *    deployment, so a regression here would silently break every
 *    Deno customer.
 *
 * # Why a separate file (not a vitest test)
 *
 * Vitest itself runs in Node, so a vitest test is a Node test by
 * definition. Cross-runtime claims need execution under the actual
 * runtime — Deno's import resolution, its `globalThis.Deno` namespace
 * (which we explicitly do NOT touch), and its `crypto.subtle` impl
 * are different code paths than Node's. CI installs Deno via
 * `denoland/setup-deno` and runs this file directly.
 */

// Deno's URL resolver requires explicit `.js` extensions for relative
// imports, which our `dist/index.js` already uses internally — we
// just import the published entry the same way a customer would.
// deno-lint-ignore no-explicit-any
const { WasmEngine, wrapFetch } = (await import("../../dist/index.js")) as any;

const policy = JSON.stringify({ agent: "deno-test", default: "allow", rules: [] });
const denyPolicy = JSON.stringify({ agent: "deno-test", default: "deny", rules: [] });

const wasmBytes = await Deno.readFile(new URL("../../checkrd_core.wasm", import.meta.url));

// ---------------------------------------------------------------------------
// 1. Engine constructs without node:* shims
// ---------------------------------------------------------------------------

const engine = await WasmEngine.create(policy, "deno-agent", { wasm: wasmBytes });
if (typeof engine.evaluate !== "function") {
  throw new Error("WasmEngine.create() did not return an engine with .evaluate()");
}

// ---------------------------------------------------------------------------
// 2. Allow-policy passes through; the result carries telemetry JSON
// ---------------------------------------------------------------------------

const allowResult = engine.evaluate({
  request_id: "deno-smoke-1",
  method: "GET",
  url: "https://example.com/",
  headers: [],
  body: null,
  timestamp: new Date().toISOString(),
  timestamp_ms: Date.now(),
});
if (!allowResult.allowed) {
  throw new Error(`allow-default policy denied (deny_reason=${allowResult.deny_reason ?? "?"})`);
}
if (typeof allowResult.telemetry_json !== "string" || allowResult.telemetry_json.length === 0) {
  throw new Error("allow result missing telemetry_json");
}

// ---------------------------------------------------------------------------
// 3. Deny-policy actually denies — proves the deny path is reachable
// ---------------------------------------------------------------------------

const denyEngine = await WasmEngine.create(denyPolicy, "deno-agent", { wasm: wasmBytes });
const denyResult = denyEngine.evaluate({
  request_id: "deno-smoke-2",
  method: "POST",
  url: "https://api.openai.com/v1/chat/completions",
  headers: [],
  body: null,
  timestamp: new Date().toISOString(),
  timestamp_ms: Date.now(),
});
if (denyResult.allowed) {
  throw new Error("deny-default policy unexpectedly allowed");
}
if (!denyResult.deny_reason) {
  throw new Error("deny result missing deny_reason");
}

// ---------------------------------------------------------------------------
// 4. Wrapped fetch round-trip — exercises the production user code path
// ---------------------------------------------------------------------------

let baseFetchCalled = false;
const fakeFetch: typeof fetch = (async (_input: RequestInfo | URL) => {
  baseFetchCalled = true;
  return new Response(JSON.stringify({ ok: true }), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}) as typeof fetch;

const wrapped = wrapFetch(fakeFetch, {
  engine,
  enforce: true,
  agentId: "deno-agent",
});
const resp = await wrapped("https://example.com/x", { method: "GET" });
if (resp.status !== 200) {
  throw new Error(`wrapped fetch returned status ${resp.status}, expected 200`);
}
if (!baseFetchCalled) {
  throw new Error("wrapped fetch did not delegate to the underlying baseFetch");
}

console.log("Deno smoke: 4/4 checks passed");
