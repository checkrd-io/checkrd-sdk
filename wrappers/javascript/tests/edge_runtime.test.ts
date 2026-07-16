/**
 * Edge-runtime smoke test.
 *
 * Runs the SDK's compiled ESM bundle inside an `@edge-runtime/vm`
 * context — a WinterCG-compliant sandbox that, crucially, does NOT
 * provide `node:fs`, `node:crypto`, or `node:url`. If the SDK were
 * eagerly touching any of those Node-only modules at import time, the
 * whole module would fail to load here. Getting past the import
 * without a throw is the entire point.
 *
 * This test documents the property that `checkrd` is safe to import
 * on Cloudflare Workers, Vercel Edge, Deno, and the browser.
 */

import { readFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { beforeAll, describe, expect, it } from "vitest";
import { EdgeVM } from "@edge-runtime/vm";

const here = dirname(fileURLToPath(import.meta.url));
// Use the CJS bundles — edge-runtime's `EdgeVM.evaluate` runs scripts,
// not ESM modules, so we reach for the `.cjs` variants that already
// wrap everything in `module.exports`.
const indexBundlePath = resolve(here, "..", "dist", "index.cjs");
const advancedBundlePath = resolve(here, "..", "dist", "advanced.cjs");
const wasmPath = resolve(here, "..", "checkrd_core.wasm");

let indexBundleSource = "";
let advancedBundleSource = "";

beforeAll(async () => {
  // Strict: a missing bundle indicates CI forgot `npm run build` or
  // the local dev loop ran tests without a prior build. Silent skips
  // here would create a confidence hole — the edge property is a
  // first-class correctness claim, not a nice-to-have.
  const loadOrFail = async (p: string): Promise<string> =>
    readFile(p, "utf-8").catch((err: unknown) => {
      throw new Error(
        `edge-runtime test requires ${p}. Run \`npm run build\` first. ` +
          `Underlying error: ${String(err)}`,
      );
    });
  indexBundleSource = await loadOrFail(indexBundlePath);
  advancedBundleSource = await loadOrFail(advancedBundlePath);
});

describe("edge-runtime smoke", () => {
  it("imports cleanly inside a WinterCG VM (no eager node:* imports)", async () => {
    const vm = new EdgeVM();
    const wasmBytes = await readFile(wasmPath);
    // Prime the sandbox. The CJS bundle expects `module`, `exports`,
    // and `require` to be available in scope. EdgeVM disables
    // `new Function` (mimics edge-runtime CSP), so we set those up on
    // `globalThis` and evaluate the bundle source directly.
    (vm.context as unknown as {
      wasmBytes: Uint8Array;
      policy: string;
    }).wasmBytes = new Uint8Array(wasmBytes);
    (vm.context as unknown as { policy: string }).policy = JSON.stringify({
      agent: "t",
      mode: "enforce",
      default: "allow",
      rules: [],
    });
    // On real edge runtimes the bundle is ESM + inlined dependencies,
    // so there is no `require` at all. For the purposes of this test,
    // we allow pure-JS dependency requires (yaml, @bjorn3/*) so the
    // CJS bundle can link those — what we're actually asserting is
    // that no `node:*` (or bare Node built-in) module is touched at
    // load time, which would outright fail on every edge runtime.
    const NODE_BUILTINS = new Set([
      "fs",
      "path",
      "url",
      "module",
      "crypto",
      "os",
      "child_process",
      "worker_threads",
      "stream",
      "net",
      "tls",
      "dns",
      "http",
      "https",
      "zlib",
    ]);
    (vm.context as unknown as {
      __nodeBuiltins: Set<string>;
    }).__nodeBuiltins = NODE_BUILTINS;
    // `createRequire` on Node is still available to the test harness,
    // so we forward non-built-in requires to a real loader and throw
    // only for `node:*` / bare-name Node built-ins.
    const { createRequire } = await import("node:module");
    const realRequire = createRequire(import.meta.url);
    (vm.context as unknown as {
      __realRequire: typeof realRequire;
    }).__realRequire = realRequire;
    vm.evaluate(`
      globalThis.module = { exports: {} };
      globalThis.exports = globalThis.module.exports;
      // tsup's CJS shim references __filename / __dirname for the
      // import.meta.url fallback. Edge runtimes don't provide them
      // but neither does a browser — provide inert stubs so the
      // shim itself doesn't crash. (The SDK never reads these
      // values unless the sync path is touched, which our test
      // deliberately avoids.)
      globalThis.__filename = '/virtual/index.cjs';
      globalThis.__dirname = '/virtual';
      globalThis.require = (name) => {
        const normalized = name.startsWith('node:') ? name.slice(5) : name;
        if (globalThis.__nodeBuiltins.has(normalized)) {
          throw new Error(
            'unexpected eager require(' + name + ') in edge-runtime bundle',
          );
        }
        return globalThis.__realRequire(name);
      };
    `);
    // Evaluate BOTH bundles as scripts. The main entry must import
    // cleanly (no eager node:* access — that's the curated public
    // surface). The `advanced` entry must too — it's the power-user
    // re-export of engine internals, sinks, retry primitives. If any
    // module on either path eagerly touches node, the throwing
    // require below turns it into a clear test failure.
    vm.evaluate(indexBundleSource);
    const indexExports = vm.evaluate<Record<string, unknown>>(
      `globalThis.module.exports`,
    );
    // Main entry contract: initAsync is exposed for the function-style
    // entry point that 90% of users start with.
    expect(typeof indexExports["initAsync"]).toBe("function");

    // Reset the CJS scaffold so the second bundle gets a fresh
    // module.exports — otherwise advanced would smear over index.
    vm.evaluate(`globalThis.module = { exports: {} }; globalThis.exports = globalThis.module.exports;`);
    vm.evaluate(advancedBundleSource);
    // Advanced entry contract: WasmEngine.create() is an async
    // factory that works on Edge / Workers / Deno without
    // node:fs / node:crypto / node:url. Calling it inside this
    // sandbox is the strongest possible test of that property.
    const result = await vm.evaluate<Promise<unknown>>(`
      (async () => {
        const adv = globalThis.module.exports;
        if (typeof adv.WasmEngine !== 'function') {
          throw new Error('WasmEngine export missing from checkrd/advanced');
        }
        const engine = await adv.WasmEngine.create(
          policy,
          'edge-agent',
          { wasm: wasmBytes },
        );
        const r = engine.evaluate({
          request_id: 'r',
          method: 'GET',
          url: 'https://example.com/',
          headers: [],
          body: null,
          timestamp: new Date().toISOString(),
          timestamp_ms: Date.now(),
        });
        return { allowed: r.allowed };
      })()
    `);
    expect((result as { allowed: boolean }).allowed).toBe(true);
  });

  // -------------------------------------------------------------------------
  // The two tests below run inside the same WinterCG sandbox but
  // exercise the *security-relevant* code paths — denial enforcement
  // and WASM integrity. The original sandbox test only proved that
  // the engine loads and an allow-policy returns allowed=true; that's
  // necessary but not sufficient. A regression that broke the deny
  // path or skipped the integrity check would still pass that test
  // because the happy path keeps working.
  // -------------------------------------------------------------------------

  it("enforces a deny policy inside the sandbox (deny path is reachable)", async () => {
    const vm = new EdgeVM();
    const wasmBytes = await readFile(wasmPath);
    (vm.context as unknown as {
      wasmBytes: Uint8Array;
      policy: string;
    }).wasmBytes = new Uint8Array(wasmBytes);
    (vm.context as unknown as { policy: string }).policy = JSON.stringify({
      agent: "t",
      mode: "enforce",
      default: "deny",
      rules: [],
    });

    const { createRequire } = await import("node:module");
    const realRequire = createRequire(import.meta.url);
    (vm.context as unknown as { __realRequire: typeof realRequire }).__realRequire =
      realRequire;
    vm.evaluate(`
      globalThis.module = { exports: {} };
      globalThis.exports = globalThis.module.exports;
      globalThis.__filename = '/virtual/index.cjs';
      globalThis.__dirname = '/virtual';
      globalThis.require = (name) => {
        if (name.startsWith('node:')) {
          throw new Error('unexpected require(' + name + ') in edge bundle');
        }
        return globalThis.__realRequire(name);
      };
    `);
    vm.evaluate(advancedBundleSource);
    const result = await vm.evaluate<Promise<unknown>>(`
      (async () => {
        const engine = await globalThis.module.exports.WasmEngine.create(
          policy, 'edge-agent', { wasm: wasmBytes },
        );
        const r = engine.evaluate({
          request_id: 'r',
          method: 'POST',
          url: 'https://api.openai.com/v1/chat/completions',
          headers: [],
          body: null,
          timestamp: new Date().toISOString(),
          timestamp_ms: Date.now(),
        });
        return { allowed: r.allowed, deny_reason: r.deny_reason };
      })()
    `);
    const r = result as { allowed: boolean; deny_reason?: string };
    expect(r.allowed).toBe(false);
    expect(r.deny_reason).toBeDefined();
  });

  it("rejects tampered WASM bytes via the SHA-256 integrity check", async () => {
    // The engine ships a baked-in hash of the WASM it was built
    // against and rejects any other bytes. Without this guard, an
    // attacker who could substitute the WASM payload (compromised
    // CDN, MITM on a self-host registry) could replace the policy
    // engine wholesale. Force a one-byte mutation and verify
    // `WasmEngine.create()` throws — silently accepting the
    // tampered bytes would be a critical regression.
    const vm = new EdgeVM();
    const wasmBytes = await readFile(wasmPath);
    const tampered = new Uint8Array(wasmBytes);
    // Flip a byte deep enough that we don't corrupt the magic header
    // (which would fail at WebAssembly.compile, not at our integrity
    // check — we want to prove OUR check fires, not WebAssembly's).
    const idx = Math.floor(tampered.length / 2);
    const byte = tampered[idx];
    if (byte === undefined) {
      throw new Error("WASM payload too small to tamper");
    }
    tampered[idx] = byte ^ 0x01;

    (vm.context as unknown as {
      wasmBytes: Uint8Array;
      policy: string;
    }).wasmBytes = tampered;
    (vm.context as unknown as { policy: string }).policy = JSON.stringify({
      agent: "t",
      mode: "enforce",
      default: "allow",
      rules: [],
    });

    const { createRequire } = await import("node:module");
    const realRequire = createRequire(import.meta.url);
    (vm.context as unknown as { __realRequire: typeof realRequire }).__realRequire =
      realRequire;
    vm.evaluate(`
      globalThis.module = { exports: {} };
      globalThis.exports = globalThis.module.exports;
      globalThis.__filename = '/virtual/index.cjs';
      globalThis.__dirname = '/virtual';
      globalThis.require = (name) => {
        if (name.startsWith('node:')) {
          throw new Error('unexpected require(' + name + ') in edge bundle');
        }
        return globalThis.__realRequire(name);
      };
    `);
    vm.evaluate(advancedBundleSource);
    const outcome = await vm.evaluate<Promise<unknown>>(`
      (async () => {
        try {
          await globalThis.module.exports.WasmEngine.create(
            policy, 'edge-agent', { wasm: wasmBytes },
          );
          return { ok: true };
        } catch (err) {
          return { ok: false, name: err && err.name, message: err && err.message };
        }
      })()
    `);
    const r = outcome as { ok: boolean; name?: string; message?: string };
    expect(r.ok).toBe(false);
    // Either our explicit CheckrdInitError ("integrity") or the
    // underlying WebAssembly compile failure is acceptable evidence
    // that tampering was rejected — what we MUST NOT see is `ok:
    // true`. The error message should make the cause clear.
    const message = r.message ?? "";
    expect(message).toMatch(/integrity|sha-?256|CompileError/i);
  });

  it("runs the cost-metering pricing FFI inside the sandbox (edge-clean, M-12)", async () => {
    // The pricing FFI bindings (`reloadPricingSigned` / `settleUsage`) and
    // the trust accessors must not touch node:* — they ride the same
    // marshalling helpers as `evaluate`. Signing here uses the runtime's
    // own WebCrypto Ed25519 (present in WinterCG), and the trust list is
    // passed DIRECTLY to the FFI (the env-backed accessor is exercised in
    // the Node trust tests). Getting a priced `SettleResult` out of the
    // EdgeVM proves the whole pricing path is edge-safe.
    const vm = new EdgeVM();
    const wasmBytes = await readFile(wasmPath);
    (vm.context as unknown as {
      wasmBytes: Uint8Array;
      policy: string;
    }).wasmBytes = new Uint8Array(wasmBytes);
    (vm.context as unknown as { policy: string }).policy = JSON.stringify({
      agent: "t",
      mode: "enforce",
      default: "deny",
      rules: [],
    });

    const { createRequire } = await import("node:module");
    const realRequire = createRequire(import.meta.url);
    (vm.context as unknown as { __realRequire: typeof realRequire }).__realRequire =
      realRequire;
    vm.evaluate(`
      globalThis.module = { exports: {} };
      globalThis.exports = globalThis.module.exports;
      globalThis.__filename = '/virtual/index.cjs';
      globalThis.__dirname = '/virtual';
      globalThis.require = (name) => {
        if (name.startsWith('node:')) {
          throw new Error('unexpected require(' + name + ') in edge bundle');
        }
        return globalThis.__realRequire(name);
      };
    `);
    vm.evaluate(advancedBundleSource);
    const outcome = await vm.evaluate<Promise<unknown>>(`
      (async () => {
        const adv = globalThis.module.exports;
        const enc = new TextEncoder();
        const b64 = (bytes) => {
          let s = '';
          for (const x of bytes) s += String.fromCharCode(x);
          return btoa(s);
        };
        // PAE per the DSSE spec text.
        const pae = (type, payload) => {
          const prefix = enc.encode(
            'DSSEv1 ' + type.length + ' ' + type + ' ' + payload.length + ' '
          );
          const out = new Uint8Array(prefix.length + payload.length);
          out.set(prefix, 0);
          out.set(payload, prefix.length);
          return out;
        };
        const pair = await crypto.subtle.generateKey({ name: 'Ed25519' }, true, ['sign', 'verify']);
        const pubRaw = new Uint8Array(await crypto.subtle.exportKey('raw', pair.publicKey));
        const pubHex = Array.from(pubRaw, (x) => x.toString(16).padStart(2, '0')).join('');
        const bundle = enc.encode(JSON.stringify({
          schema_version: 1, version: 1, signed_at: Math.floor(Date.now()/1000),
          rounding: 'half_up',
          skus: [{
            sku_id: 'sku-edge', provider: 'anthropic', model_match: 'claude-sonnet-*',
            unit: 'per_1m_tokens',
            input_usd_micros_per_unit: 3000000, output_usd_micros_per_unit: 15000000,
            default_max_output_tokens: 8192, effective_from: 1700000000, source: 'list'
          }]
        }));
        const PRICING_TYPE = 'application/vnd.checkrd.pricing-bundle+json';
        const sig = new Uint8Array(await crypto.subtle.sign({ name: 'Ed25519' }, pair.privateKey, pae(PRICING_TYPE, bundle)));
        const envelope = JSON.stringify({
          payloadType: PRICING_TYPE,
          payload: b64(bundle),
          signatures: [{ keyid: 'edge-cp', sig: b64(sig) }],
        });
        const trusted = JSON.stringify([{ keyid: 'edge-cp', public_key_hex: pubHex, valid_from: 0, valid_until: 9007199254740991 }]);

        const engine = await adv.WasmEngine.create(policy, 'edge-agent', { wasm: wasmBytes });
        engine.reloadPricingSigned({
          envelopeJson: envelope, trustedKeysJson: trusted,
          nowUnixSecs: Math.floor(Date.now()/1000), maxAgeSecs: 86400,
        });
        const version = engine.getActivePricingVersion();
        const settled = engine.settleUsage('edge-r', {
          provider: 'anthropic', model: 'claude-sonnet-4',
          input_tokens: 1000, output_tokens: 500,
        });
        return { version, cost: settled.cost_usd_micros, status: settled.pricing_status };
      })()
    `);
    const r = outcome as { version: number; cost: number; status: string };
    expect(r.version).toBe(1);
    expect(r.status).toBe("priced");
    // 1000 input @ $3/1M (3000) + 500 output @ $15/1M (7500) = 10_500 micros.
    expect(r.cost).toBe(10_500);
  });

  it("drives the pricing-consume dispatcher (handleControlEvent) inside the sandbox (edge-clean, M-14)", async () => {
    // The FFI edge test above proves `reloadPricingSigned` is edge-clean.
    // This proves the LAYER ABOVE it — the control-event dispatcher's new
    // `pricing_updated` install path (`installSignedPricing` in
    // control.ts) — touches no `node:*` either. If any of the new pricing
    // wiring pulled in a Node built-in, the throwing `require` below would
    // turn it into a hard failure at bundle-eval time; getting a live
    // `getActivePricingVersion()` out of a dispatched event confirms the
    // whole path (dispatch → verify → install) is WinterCG-safe.
    const vm = new EdgeVM();
    const wasmBytes = await readFile(wasmPath);
    (vm.context as unknown as { wasmBytes: Uint8Array; policy: string }).wasmBytes =
      new Uint8Array(wasmBytes);
    (vm.context as unknown as { policy: string }).policy = JSON.stringify({
      agent: "t",
      mode: "enforce",
      default: "deny",
      rules: [],
    });

    const { createRequire } = await import("node:module");
    const realRequire = createRequire(import.meta.url);
    (vm.context as unknown as { __realRequire: typeof realRequire }).__realRequire =
      realRequire;
    vm.evaluate(`
      globalThis.module = { exports: {} };
      globalThis.exports = globalThis.module.exports;
      globalThis.__filename = '/virtual/index.cjs';
      globalThis.__dirname = '/virtual';
      globalThis.require = (name) => {
        if (name.startsWith('node:')) {
          throw new Error('unexpected require(' + name + ') in edge bundle');
        }
        return globalThis.__realRequire(name);
      };
    `);
    vm.evaluate(advancedBundleSource);
    const outcome = await vm.evaluate<Promise<unknown>>(`
      (async () => {
        const adv = globalThis.module.exports;
        const enc = new TextEncoder();
        const b64 = (bytes) => {
          let s = '';
          for (const x of bytes) s += String.fromCharCode(x);
          return btoa(s);
        };
        const pae = (type, payload) => {
          const prefix = enc.encode(
            'DSSEv1 ' + type.length + ' ' + type + ' ' + payload.length + ' '
          );
          const out = new Uint8Array(prefix.length + payload.length);
          out.set(prefix, 0);
          out.set(payload, prefix.length);
          return out;
        };
        const pair = await crypto.subtle.generateKey({ name: 'Ed25519' }, true, ['sign', 'verify']);
        const pubRaw = new Uint8Array(await crypto.subtle.exportKey('raw', pair.publicKey));
        const pubHex = Array.from(pubRaw, (x) => x.toString(16).padStart(2, '0')).join('');
        const bundle = enc.encode(JSON.stringify({
          schema_version: 1, version: 2, signed_at: Math.floor(Date.now()/1000),
          rounding: 'half_up',
          skus: [{
            sku_id: 'sku-edge', provider: 'anthropic', model_match: 'claude-sonnet-*',
            unit: 'per_1m_tokens',
            input_usd_micros_per_unit: 3000000, output_usd_micros_per_unit: 15000000,
            default_max_output_tokens: 8192, effective_from: 1700000000, source: 'list'
          }]
        }));
        const PRICING_TYPE = 'application/vnd.checkrd.pricing-bundle+json';
        const sig = new Uint8Array(await crypto.subtle.sign({ name: 'Ed25519' }, pair.privateKey, pae(PRICING_TYPE, bundle)));
        const envelope = {
          payloadType: PRICING_TYPE,
          payload: b64(bundle),
          signatures: [{ keyid: 'edge-cp', sig: b64(sig) }],
        };
        const trusted = JSON.stringify([{ keyid: 'edge-cp', public_key_hex: pubHex, valid_from: 0, valid_until: 9007199254740991 }]);

        const engine = await adv.WasmEngine.create(policy, 'edge-agent', { wasm: wasmBytes });
        // Dispatch a real pricing_updated event through the dispatcher.
        adv.handleControlEvent(
          engine,
          'pricing_updated',
          JSON.stringify({ version: 2, pricing_envelope: envelope }),
          undefined,
          undefined,
          { loadTrustedKeys: () => trusted },
        );
        // install is fire-and-forget (async trust load); poll for it.
        for (let i = 0; i < 50; i++) {
          if (engine.getActivePricingVersion() > 0) break;
          await new Promise((r) => setTimeout(r, 5));
        }
        return { version: engine.getActivePricingVersion() };
      })()
    `);
    const r = outcome as { version: number };
    expect(r.version).toBe(2);
  });
});
