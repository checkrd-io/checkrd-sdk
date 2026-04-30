/**
 * Policy loading — accepts a file path, YAML/JSON string, or object, and
 * returns canonical JSON suitable for the WASM `init()` call. Mirrors
 * `checkrd/config.py`.
 *
 * `node:fs` is loaded lazily via `require()` the first time a file-path
 * policy is passed. That keeps the module parseable on Cloudflare
 * Workers, Vercel Edge, Deno, and the browser — runtimes where
 * `node:fs` does not exist. File-path policies themselves don't work
 * there (there's no filesystem), but runtimes that only pass
 * inline-object / inline-YAML policies load cleanly.
 */
import { parse as parseYaml } from "yaml";

import { readEnv } from "./_env.js";
import { CheckrdInitError } from "./exceptions.js";

interface NodeFsShim {
  readFileSync(path: string, encoding: "utf-8"): string;
}

let _fs: NodeFsShim | null = null;
function loadNodeFs(): NodeFsShim {
  if (_fs) return _fs;
  try {
    // eslint-disable-next-line @typescript-eslint/no-require-imports -- sync load on Node
    _fs = require("node:fs") as NodeFsShim;
    return _fs;
  } catch {
    throw new CheckrdInitError(
      "policy file loading requires node:fs, which is not available " +
        "in this runtime (Cloudflare Workers, Vercel Edge, Deno, " +
        "browser). Pass the policy inline as a string or object " +
        "instead of a file path.",
    );
  }
}

/** Accepted shapes for the `policy` parameter of `wrap()` / `init()`. */
export type PolicyInput = string | Record<string, unknown> | null | undefined;

/**
 * Resolve a policy input into canonical JSON.
 *
 * Heuristic for string inputs: a single-line string that does not start
 * with `{` or `[` is treated as a file path; everything else is parsed
 * as YAML (a superset of JSON, so pure JSON strings also work).
 */
export function loadConfig(policy: PolicyInput): string {
  if (policy === null || policy === undefined) {
    const envPath = readEnv("CHECKRD_POLICY_FILE");
    if (envPath !== undefined) {
      return loadFromFile(envPath);
    }
    throw new CheckrdInitError("no policy configured");
  }

  if (typeof policy === "object") {
    return JSON.stringify(policy);
  }

  const trimmed = policy.trim();
  const looksLikeContent =
    policy.includes("\n") || trimmed.startsWith("{") || trimmed.startsWith("[");

  if (!looksLikeContent) {
    // Path branch: read, parse, canonicalize.
    return loadFromFile(policy);
  }

  // Content branch: parse directly.
  try {
    const parsed = parseYaml(policy) as unknown;
    return JSON.stringify(parsed);
  } catch (err) {
    throw new CheckrdInitError(
      `invalid policy: ${err instanceof Error ? err.message : String(err)}`,
    );
  }
}

function loadFromFile(path: string): string {
  let content: string;
  try {
    content = loadNodeFs().readFileSync(path, "utf-8");
  } catch (err) {
    if (err instanceof CheckrdInitError) throw err;
    throw new CheckrdInitError(
      `policy file not found or unreadable: ${path} (${
        err instanceof Error ? err.message : String(err)
      })`,
    );
  }
  try {
    const parsed = parseYaml(content) as unknown;
    return JSON.stringify(parsed);
  } catch (err) {
    throw new CheckrdInitError(
      `invalid policy at ${path}: ${
        err instanceof Error ? err.message : String(err)
      }`,
    );
  }
}
