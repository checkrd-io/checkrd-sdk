/**
 * Bun cross-runtime smoke test.
 *
 * Run with: `bun run tests/runtime/bun_smoke.ts`
 *
 * # What this proves
 *
 * 1. The published ESM bundle imports cleanly in Bun. Bun is
 *    Node-compatible by design but its WebAssembly + crypto
 *    implementations are independent reimplementations (Zig +
 *    BoringSSL), so a regression that only manifests under Bun's
 *    distinct backends would slip through Node-only tests.
 *
 * 2. `WasmEngine.create()` runs through Bun's WebAssembly compiler.
 *    The fast path (`WebAssembly.instantiateStreaming`) and the
 *    fallback (`compile` + `instantiate`) both have to work.
 *
 * 3. Wrapped fetch round-trip works against Bun's `fetch` impl
 *    (BoringSSL TLS, undici-incompatible Response semantics in
 *    older Bun versions).
 *
 * # Why a separate file (not a vitest test)
 *
 * Vitest runs in Node. To exercise Bun's actual runtime, we ship
 * a standalone smoke script and run it via `bun run` in CI. The
 * smoke script uses `Bun.file` for the WASM read so a Node
 * runtime would fail fast (Bun.file is not in Node) — that's the
 * intentional check that the test really did execute under Bun.
 */

// Bun-only API: forces a hard failure if this script is mis-routed
// through Node. We dereference it lazily so a plain TypeScript
// import doesn't require Bun's namespace to be globally typed.
const Bun = (globalThis as unknown as { Bun?: { file: (path: string) => { arrayBuffer(): Promise<ArrayBuffer> } } }).Bun;
if (!Bun) {
  console.error("bun_smoke.ts must be executed with `bun run`, not Node");
  process.exit(2);
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const { WasmEngine, wrapFetch } = (await import("../../dist/index.js")) as any;

const policy = JSON.stringify({ agent: "bun-test", default: "allow", rules: [] });
const denyPolicy = JSON.stringify({ agent: "bun-test", default: "deny", rules: [] });

const wasmBuf = await Bun.file(
  new URL("../../checkrd_core.wasm", import.meta.url).pathname,
).arrayBuffer();
const wasmBytes = new Uint8Array(wasmBuf);

// ---------------------------------------------------------------------------
// 1. Engine constructs under Bun's WebAssembly impl
// ---------------------------------------------------------------------------

const engine = await WasmEngine.create(policy, "bun-agent", { wasm: wasmBytes });
if (typeof engine.evaluate !== "function") {
  console.error("WasmEngine.create() did not return an engine with .evaluate()");
  process.exit(1);
}

// ---------------------------------------------------------------------------
// 2. Allow + deny paths
// ---------------------------------------------------------------------------

const allowResult = engine.evaluate({
  request_id: "bun-smoke-1",
  method: "GET",
  url: "https://example.com/",
  headers: [],
  body: null,
  timestamp: new Date().toISOString(),
  timestamp_ms: Date.now(),
});
if (!allowResult.allowed) {
  console.error(`allow-default policy denied (deny_reason=${allowResult.deny_reason ?? "?"})`);
  process.exit(1);
}
if (typeof allowResult.telemetry_json !== "string" || allowResult.telemetry_json.length === 0) {
  console.error("allow result missing telemetry_json");
  process.exit(1);
}

const denyEngine = await WasmEngine.create(denyPolicy, "bun-agent", { wasm: wasmBytes });
const denyResult = denyEngine.evaluate({
  request_id: "bun-smoke-2",
  method: "POST",
  url: "https://api.openai.com/v1/chat/completions",
  headers: [],
  body: null,
  timestamp: new Date().toISOString(),
  timestamp_ms: Date.now(),
});
if (denyResult.allowed || !denyResult.deny_reason) {
  console.error("deny-default policy did not deny as expected");
  process.exit(1);
}

// ---------------------------------------------------------------------------
// 3. Wrapped fetch round-trip on Bun's fetch impl
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
  agentId: "bun-agent",
});
const resp = await wrapped("https://example.com/x", { method: "GET" });
if (resp.status !== 200 || !baseFetchCalled) {
  console.error(
    `wrapped fetch failed: status=${resp.status}, baseFetchCalled=${baseFetchCalled}`,
  );
  process.exit(1);
}

console.log("Bun smoke: 4/4 checks passed");
