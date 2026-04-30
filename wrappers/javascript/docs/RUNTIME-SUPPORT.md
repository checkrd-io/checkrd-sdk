# Runtime support — single bundle, runtime-detected

## Decision

Checkrd ships **one** bundle per module format (one ESM + one CJS) and
detects the runtime at load time. The package.json `exports` map
declares explicit support for the major non-Node runtimes by adding
`workerd`, `deno`, `bun`, and `browser` conditions, all pointing at
the same ESM entry — runtime-conditional code paths inside the bundle
do the actual differentiation.

## Why one bundle, not five

Two industry-standard patterns exist for multi-runtime SDKs in 2026:

1. **Stripe pattern**: separate physical bundles per runtime
   (`stripe.esm.node.js`, `stripe.esm.worker.js`, `stripe.browser.js`).
   The build emits N artifacts; `package.json#exports` routes each
   runtime to the right one.
2. **Stainless pattern** (OpenAI, Anthropic, Vercel AI SDK): one
   bundle with internal runtime detection. The bundle uses lazy
   imports for runtime-specific APIs (`await import("node:fs")` only
   on Node) and feature-detects Web APIs (`globalThis.crypto.subtle`)
   for the rest.

We use the Stainless pattern. The Checkrd `WasmEngine` already
implements runtime detection — `WasmEngine.create()` (async) uses
`WebAssembly.instantiateStreaming` for edge runtimes; the sync
constructor uses `node:fs` only when running on Node. Splitting into
separate bundles would buy:

- ~2-3 KB of bundle-size reduction on the edge bundle (the `node:fs`
  import is dynamic and tree-shakable already, so the actual delta is
  small).
- Marginally clearer error messages when someone uses a Node-only
  path on an edge runtime.

The cost would be:

- Two builds to maintain — every change has to pass tsup twice.
- Two `size-limit` budgets, two `attw` runs, two `publint` runs.
- A whole class of "works in CI, breaks in production" bugs when the
  test harness runs the Node bundle but the deploy runs the edge one.
- Source-map stitching across bundles for stack traces.

Stainless's experience scaled across 100+ SDKs is that the
single-bundle pattern is meaningfully simpler with no observable cost
to the consumer.

## What the export conditions buy

Build tooling (esbuild, Vite, Rollup, tsdown, webpack 5, Turbopack)
all read the `exports` map and pick the most specific matching
condition. By naming `workerd`, `deno`, `bun`, and `browser`
explicitly:

- A consumer running `wrangler dev` (`workerd` runtime) will resolve
  the ESM entry — no warning about missing CJS support.
- A consumer running `deno task` resolves the ESM entry directly,
  not through the `import` fallback.
- A bundler targeting the browser sees the `browser` condition and
  knows we have first-class support (we still throw at runtime via
  `dangerouslyAllowBrowser` checks — see `SECURITY.md`).
- Node consumers fall through to `import` / `require` as before.

The conditions document the support matrix in machine-readable form
that toolchains can inspect; `arethetypeswrong` validates that the
chain resolves cleanly for every condition.

## When we will split into multiple bundles

Three signals would justify the move to per-runtime bundles:

1. **Bundle size**: if the edge variant exceeds 60 KB gzipped (the
   current size-limit budget for the main bundle), splitting becomes
   net-positive.
2. **Runtime divergence**: if Workers requires a fundamentally
   different fetch implementation than Node (today both use
   `globalThis.fetch`), the divergence forces separate code paths.
3. **Cold-start latency**: if the runtime-detection branch costs
   measurable cold-start time on edge isolates, pre-baked bundles
   skip the branch.

None of these signals fire today. Re-evaluate annually.

## Verifying compatibility

Each release runs the test suite under `@edge-runtime/vm`
(Cloudflare Workers / Vercel Edge environment) in addition to Node.
See `vitest.config.ts` for the dual-runtime configuration.
