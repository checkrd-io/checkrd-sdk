/**
 * Built-artifact regression test — native Node ESM (`require`-less scope).
 *
 * `src/webhooks.ts` (`verifyWebhook`) and `src/config.ts` (`loadConfig`
 * file path) synchronously load `node:crypto` / `node:fs`. A bare
 * `require("node:...")` in source is silently rewritten by tsup/esbuild
 * into a `__require(...)` shim that THROWS `Dynamic require ... is not
 * supported` whenever the ambient `require` is undefined — which is
 * ALWAYS the case in a native Node ESM module (Next.js server, `.mjs`,
 * `"type":"module"`). Source-level unit tests can't see this: vitest
 * supplies a `require`, so the throw never fires against `src/`.
 *
 * This test therefore exercises the SHIPPED artifact (`dist/index.js`,
 * `dist/advanced.js`) inside a genuine `node --input-type=module`
 * subprocess where `require` is guaranteed undefined at module scope —
 * the exact runtime the bug lives in. It fails hard before the
 * `resolveBuiltin` (getBuiltinModule-first) fix and passes after.
 *
 * Complements `tests/edge_runtime.test.ts` (WinterCG / no-`node:*`
 * invariant) and `scripts/install-sanity.mjs` (post-pack ESM invocation).
 */
import { execFileSync } from "node:child_process";
import { createHmac } from "node:crypto";
import { existsSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { beforeAll, describe, expect, it } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const PKG_ROOT = resolve(here, "..");
const indexDist = resolve(PKG_ROOT, "dist", "index.js");
const advancedDist = resolve(PKG_ROOT, "dist", "advanced.js");

// Strict, matching tests/edge_runtime.test.ts: the built artifact is the
// system under test. A missing bundle means `npm run build` was skipped;
// silently skipping would hide the exact regression this file exists to
// catch — the Node-ESM property is a first-class correctness claim.
beforeAll(() => {
  for (const p of [indexDist, advancedDist]) {
    if (!existsSync(p)) {
      throw new Error(
        `node-ESM built-artifact test requires ${p}. Run \`npm run build\` first.`,
      );
    }
  }
});

interface NodeRun {
  status: number;
  stdout: string;
  stderr: string;
}

/**
 * Run `script` as a real Node ESM module in a child process. `require`
 * is undefined at module scope there — precisely the condition that
 * turns esbuild's `__require` shim into a throw. No shell is spawned
 * (argv array form), so the interpolated `JSON.stringify(...)` values
 * are the only escaping needed.
 */
function runNodeEsm(script: string): NodeRun {
  try {
    const stdout = execFileSync(
      process.execPath,
      ["--input-type=module", "--eval", script],
      { encoding: "utf-8" },
    );
    return { status: 0, stdout, stderr: "" };
  } catch (err) {
    const e = err as {
      status?: number | null;
      stdout?: Buffer | string | null;
      stderr?: Buffer | string | null;
    };
    return {
      status: e.status ?? 1,
      stdout: e.stdout?.toString() ?? "",
      stderr: e.stderr?.toString() ?? "",
    };
  }
}

/** Last non-empty stdout line — the JSON result the child prints. */
function lastLine(s: string): string {
  return s.trim().split("\n").filter((l) => l.trim().length > 0).at(-1) ?? "";
}

describe("built artifact runs under native Node ESM (no ambient require)", () => {
  it("verifyWebhook (dist/index.js) verifies a valid HMAC and rejects a tampered body", () => {
    const indexUrl = pathToFileURL(indexDist).href;
    const secret = "whsec_node_esm_artifact";
    const body = JSON.stringify({ event: "policy.updated", version: 7 });
    const ts = 1_700_000_000;
    const hex = createHmac("sha256", secret)
      .update(`${ts.toString()}.${body}`)
      .digest("hex");
    const header = `t=${ts.toString()},v1=${hex}`;

    // The child imports the BUILT curated entry and actually invokes
    // verifyWebhook. A valid signature must return; a tampered body must
    // be rejected with `signature_mismatch` — proving the real
    // node:crypto HMAC ran, not a spurious pass.
    const script = `
      const { verifyWebhook } = await import(${JSON.stringify(indexUrl)});
      const opts = {
        rawBody: ${JSON.stringify(body)},
        signatureHeader: ${JSON.stringify(header)},
        secret: ${JSON.stringify(secret)},
        nowUnixSecs: () => ${ts.toString()},
      };
      verifyWebhook(opts);
      let code = null;
      try {
        verifyWebhook({ ...opts, rawBody: ${JSON.stringify(body)} + "TAMPER" });
      } catch (e) { code = e && e.code; }
      console.log(JSON.stringify({ ok: true, code }));
    `;

    const { status, stdout, stderr } = runNodeEsm(script);
    expect(
      status,
      `node ESM verifyWebhook against dist/index.js exited ${status.toString()}:\n${stderr}`,
    ).toBe(0);
    const parsed = JSON.parse(lastLine(stdout)) as {
      ok: boolean;
      code: string | null;
    };
    expect(parsed.ok).toBe(true);
    expect(parsed.code).toBe("signature_mismatch");
  });

  it("loadConfig (dist/advanced.js) reads a policy file through node:fs", () => {
    const advancedUrl = pathToFileURL(advancedDist).href;
    const dir = mkdtempSync(join(tmpdir(), "checkrd-node-esm-cfg-"));
    const policyPath = join(dir, "policy.yaml");
    writeFileSync(policyPath, "agent: node-esm\ndefault: allow\nrules: []\n");
    try {
      const script = `
        const { loadConfig } = await import(${JSON.stringify(advancedUrl)});
        const json = loadConfig(${JSON.stringify(policyPath)});
        console.log(json);
      `;
      const { status, stdout, stderr } = runNodeEsm(script);
      expect(
        status,
        `node ESM loadConfig against dist/advanced.js exited ${status.toString()}:\n${stderr}`,
      ).toBe(0);
      const parsed = JSON.parse(lastLine(stdout)) as {
        agent?: string;
        default?: string;
      };
      expect(parsed.agent).toBe("node-esm");
      expect(parsed.default).toBe("allow");
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});
