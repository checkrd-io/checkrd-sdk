/**
 * Contract test: the embedded `checkrd_core.wasm` exports EXACTLY the 17
 * named FFI functions the `WasmExports` interface binds (M-12 raised this
 * from 13 → 17 by adding the cost-metering quartet). Pins the FFI surface
 * so a WASM rebuild that drops/renames an export, or a wrapper that forgets
 * to bind a new one, fails loudly here rather than at the first call site.
 *
 * The count excludes `memory` and the WASI lifecycle hooks (`_start` /
 * `_initialize`), which are runtime plumbing, not part of the Checkrd FFI.
 */
import { readFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { beforeAll, describe, expect, it } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const wasmPath = resolve(here, "..", "checkrd_core.wasm");

// The 17 Checkrd FFI exports. Order-independent — compared as a set.
const EXPECTED_FFI_EXPORTS: readonly string[] = [
  // Memory + lifecycle
  "alloc",
  "dealloc",
  "init",
  // Evaluation + kill switch
  "evaluate_request",
  "set_kill_switch",
  // Identity / signing
  "generate_keypair",
  "derive_public_key",
  "sign",
  "sign_telemetry_batch",
  // Policy reload (DSSE)
  "reload_policy",
  "reload_policy_signed",
  "get_active_policy_version",
  "set_initial_policy_version",
  // Cost metering — pricing quartet (M-12, NEW)
  "reload_pricing_signed",
  "get_active_pricing_version",
  "set_initial_pricing_version",
  "settle_usage",
];

let exportNames: Set<string>;

beforeAll(async () => {
  const bytes = await readFile(wasmPath);
  const mod = await WebAssembly.compile(bytes);
  exportNames = new Set(
    WebAssembly.Module.exports(mod)
      .map((e) => e.name)
      // Drop runtime plumbing: linear memory + WASI lifecycle hooks.
      .filter((n) => n !== "memory" && !n.startsWith("_")),
  );
});

describe("WASM FFI export contract", () => {
  it("exports exactly 17 Checkrd FFI functions", () => {
    expect(exportNames.size).toBe(17);
    expect(EXPECTED_FFI_EXPORTS).toHaveLength(17);
  });

  it("exports exactly the expected set (no missing, no extra)", () => {
    expect([...exportNames].sort()).toEqual([...EXPECTED_FFI_EXPORTS].sort());
  });

  it.each([
    "reload_pricing_signed",
    "get_active_pricing_version",
    "set_initial_pricing_version",
    "settle_usage",
  ])("exports the cost-metering function %s (M-12)", (name) => {
    expect(exportNames.has(name)).toBe(true);
  });
});
