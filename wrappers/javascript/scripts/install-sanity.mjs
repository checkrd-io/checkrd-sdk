#!/usr/bin/env node
/**
 * Install-sanity smoke test — the JS analogue of the Python publish job's
 * "fresh venv, pip install wheel, import checkrd" step.
 *
 * Runs `npm pack`, installs the resulting tarball in a fresh temp dir,
 * and exercises both ESM and CJS resolution plus subpath exports. Fails
 * loudly if dual-publish metadata is broken in a way the unit tests
 * can't detect.
 */
import { execSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const PACKAGE_ROOT = resolve(__dirname, "..");

const run = (cmd, opts = {}) =>
  execSync(cmd, { stdio: "inherit", ...opts });

const runCapture = (cmd, opts = {}) =>
  execSync(cmd, { encoding: "utf-8", ...opts });

console.log("→ npm pack");
const packOutput = runCapture("npm pack --json", { cwd: PACKAGE_ROOT });
const packed = JSON.parse(packOutput);
const tgz = join(PACKAGE_ROOT, packed[0].filename);
console.log(`  packed: ${packed[0].filename}`);

const tmp = mkdtempSync(join(tmpdir(), "checkrd-smoke-"));
let failed = false;

try {
  writeFileSync(
    join(tmp, "package.json"),
    JSON.stringify({ name: "smoke-test", version: "1.0.0", type: "module" }),
  );

  console.log(`→ npm install ${packed[0].filename}`);
  run(`npm install --silent --no-save "${tgz}"`, { cwd: tmp });

  console.log("→ ESM import (main entry — curated public surface)");
  writeFileSync(
    join(tmp, "esm.mjs"),
    `
// Main entry exposes only the slim curated set: client class, init
// helpers, errors, webhook verifiers. WasmEngine / loadConfig
// / sinks live on the checkrd/advanced subpath (see Stripe / OpenAI pattern).
import { createHmac } from "node:crypto";
import { wrap, wrapFetch, init, Checkrd, CheckrdPolicyDenied, verifyWebhook, verifyWebhookAsync } from "checkrd";
for (const [name, val] of Object.entries({ wrap, wrapFetch, init, Checkrd, CheckrdPolicyDenied, verifyWebhook, verifyWebhookAsync })) {
  if (val === undefined) {
    throw new Error(\`ESM main-entry export missing: \${name}\`);
  }
}
// INVOKE verifyWebhook against the INSTALLED artifact under real Node ESM
// (\`require\` is undefined at module scope). This is the exact call that
// regressed to a hard throw ("Dynamic require of node:crypto ..."): a
// pure \`!== undefined\` export-presence check could never catch it, which
// is how the bug shipped. Assert a valid signature verifies AND a
// tampered body is rejected — proving the real node:crypto HMAC ran.
const secret = "whsec_install_sanity";
const body = JSON.stringify({ event: "policy.updated", version: 1 });
const ts = 1700000000;
const hex = createHmac("sha256", secret).update(\`\${ts}.\${body}\`).digest("hex");
const header = \`t=\${ts},v1=\${hex}\`;
verifyWebhook({ rawBody: body, signatureHeader: header, secret, nowUnixSecs: () => ts });
let rejectedCode = null;
try {
  verifyWebhook({ rawBody: body + "TAMPER", signatureHeader: header, secret, nowUnixSecs: () => ts });
} catch (err) {
  rejectedCode = err && err.code;
}
if (rejectedCode !== "signature_mismatch") {
  throw new Error("verifyWebhook failed to reject a tampered body (code=" + rejectedCode + ")");
}
console.log("ok: ESM main-entry exports resolve + verifyWebhook verifies through node:crypto");
`,
  );
  run("node esm.mjs", { cwd: tmp });

  console.log("→ ESM import (advanced subpath — power-user surface)");
  writeFileSync(
    join(tmp, "esm-advanced.mjs"),
    `
import { mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { WasmEngine, TelemetryBatcher, loadConfig, CircuitBreaker } from "checkrd/advanced";
for (const [name, val] of Object.entries({ WasmEngine, TelemetryBatcher, loadConfig, CircuitBreaker })) {
  if (val === undefined) {
    throw new Error(\`ESM advanced-subpath export missing: \${name}\`);
  }
}
// INVOKE loadConfig against a real file through the INSTALLED artifact
// under Node ESM. This is the exact path that regressed to a hard throw
// ("policy file loading requires node:fs ..."): a \`!== undefined\` check
// never reaches it because \`require("node:fs")\` compiled to esbuild's
// throwing __require shim.
const dir = mkdtempSync(join(tmpdir(), "checkrd-smoke-policy-"));
const policyPath = join(dir, "policy.yaml");
writeFileSync(policyPath, "agent: smoke\\ndefault: allow\\nrules: []\\n");
try {
  const canonical = loadConfig(policyPath);
  const parsed = JSON.parse(canonical);
  if (parsed.agent !== "smoke" || parsed.default !== "allow") {
    throw new Error("loadConfig returned unexpected JSON: " + canonical);
  }
} finally {
  rmSync(dir, { recursive: true, force: true });
}
console.log("ok: ESM advanced-subpath exports resolve + loadConfig reads a file through node:fs");
`,
  );
  run("node esm-advanced.mjs", { cwd: tmp });

  console.log("→ CJS require");
  writeFileSync(
    join(tmp, "cjs.cjs"),
    `
const { wrap, wrapFetch, init, Checkrd, CheckrdPolicyDenied, verifyWebhook, verifyWebhookAsync } = require("checkrd");
for (const [name, val] of Object.entries({ wrap, wrapFetch, init, Checkrd, CheckrdPolicyDenied, verifyWebhook, verifyWebhookAsync })) {
  if (val === undefined) {
    throw new Error(\`CJS main-entry export missing: \${name}\`);
  }
}
const { WasmEngine: WE_CJS } = require("checkrd/advanced");
if (typeof WE_CJS !== "function") {
  throw new Error("WasmEngine missing from checkrd/advanced (CJS)");
}
console.log("ok: CJS main + advanced exports resolve");
`,
  );
  run("node cjs.cjs", { cwd: tmp });

  console.log("→ Subpath exports (ESM)");
  writeFileSync(
    join(tmp, "subpath-esm.mjs"),
    `
import { OpenAIInstrumentor } from "checkrd/openai";
import { AnthropicInstrumentor } from "checkrd/anthropic";
if (typeof OpenAIInstrumentor !== "function") {
  throw new Error("OpenAIInstrumentor missing from checkrd/openai");
}
if (typeof AnthropicInstrumentor !== "function") {
  throw new Error("AnthropicInstrumentor missing from checkrd/anthropic");
}
console.log("ok: subpath exports resolve");
`,
  );
  run("node subpath-esm.mjs", { cwd: tmp });

  console.log("→ Subpath exports (CJS)");
  writeFileSync(
    join(tmp, "subpath-cjs.cjs"),
    `
const { OpenAIInstrumentor } = require("checkrd/openai");
const { AnthropicInstrumentor } = require("checkrd/anthropic");
if (typeof OpenAIInstrumentor !== "function") {
  throw new Error("OpenAIInstrumentor missing from checkrd/openai (CJS)");
}
if (typeof AnthropicInstrumentor !== "function") {
  throw new Error("AnthropicInstrumentor missing from checkrd/anthropic (CJS)");
}
console.log("ok: subpath CJS exports resolve");
`,
  );
  run("node subpath-cjs.cjs", { cwd: tmp });

  console.log("→ End-to-end smoke — ESM (async WasmEngine.create)");
  writeFileSync(
    join(tmp, "e2e.mjs"),
    `
process.env.CHECKRD_SKIP_WASM_INTEGRITY = "1";
// WasmEngine lives on the checkrd/advanced subpath — power-user
// surface, not part of the curated main entry. ESM consumers use
// the async factory; the sync constructor is documented as CJS-only
// because it depends on the legacy require() global to load node:fs.
import { WasmEngine } from "checkrd/advanced";
const policy = JSON.stringify({ agent: "smoke", default: "allow", rules: [] });
const engine = await WasmEngine.create(policy, "smoke");
const res = engine.evaluate({
  request_id: "smoke-1", method: "GET", url: "https://example.com/",
  headers: [], body: null, timestamp: new Date().toISOString(), timestamp_ms: Date.now(),
});
if (res.allowed !== true) throw new Error("expected allowed=true, got " + JSON.stringify(res));
console.log("ok: ESM engine evaluates through FFI end-to-end");
`,
  );
  run("node e2e.mjs", { cwd: tmp });

  console.log("→ End-to-end smoke — CJS (sync new WasmEngine)");
  writeFileSync(
    join(tmp, "e2e.cjs"),
    `
process.env.CHECKRD_SKIP_WASM_INTEGRITY = "1";
const { WasmEngine } = require("checkrd/advanced");
const policy = JSON.stringify({ agent: "smoke", default: "allow", rules: [] });
const engine = new WasmEngine(policy, "smoke");
const res = engine.evaluate({
  request_id: "smoke-1", method: "GET", url: "https://example.com/",
  headers: [], body: null, timestamp: new Date().toISOString(), timestamp_ms: Date.now(),
});
if (res.allowed !== true) throw new Error("expected allowed=true, got " + JSON.stringify(res));
console.log("ok: CJS engine evaluates through FFI end-to-end");
`,
  );
  run("node e2e.cjs", { cwd: tmp });

  console.log("");
  console.log("✓ install-sanity: all checks passed");
} catch (err) {
  console.error("✗ install-sanity FAILED");
  console.error(err);
  failed = true;
} finally {
  rmSync(tmp, { recursive: true, force: true });
  rmSync(tgz, { force: true });
  if (failed) process.exit(1);
}
