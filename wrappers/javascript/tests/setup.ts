/**
 * Vitest global setup. Mirrors the Python `_dev_mode` autouse fixture:
 *   - `CHECKRD_SKIP_WASM_INTEGRITY=1` lets tests run without the CI-generated
 *     hash file (_wasm_integrity.ts ships as an empty placeholder in the
 *     source tree).
 *   - `CHECKRD_ALLOW_INSECURE_HTTP=1` lets tests use http://localhost control
 *     plane URLs without triggering production-mode guards (parity with
 *     the Python wrapper).
 */
process.env["CHECKRD_SKIP_WASM_INTEGRITY"] = "1";
process.env["CHECKRD_ALLOW_INSECURE_HTTP"] = "1";
