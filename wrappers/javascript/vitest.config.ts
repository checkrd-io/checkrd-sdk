import { defineConfig } from "vitest/config";

/**
 * Vitest config mirrors the Python wrapper's coverage floor (80%) so both
 * SDKs publish to the same correctness bar. `forks` pool isolates each
 * test file in its own worker — necessary because the WASM module cache
 * is global and a poorly-contained test could otherwise leak state.
 */
export default defineConfig({
  test: {
    environment: "node",
    pool: "forks",
    setupFiles: ["./tests/setup.ts"],
    // Two naming conventions coexist in this repo. ``*.test.ts`` is the
    // dominant pattern for top-level tests; ``tests/integrations/test_*.ts``
    // mirrors the Python wrapper's ``tests/integrations/test_*.py`` layout
    // so both SDKs feel identical to anyone moving between them.
    include: ["tests/**/*.test.ts", "tests/integrations/test_*.ts"],
    testTimeout: 30_000,
    coverage: {
      provider: "v8",
      reporter: ["text", "lcov"],
      include: ["src/**/*.ts"],
      exclude: [
        "src/_wasm_integrity.ts",
        "src/_version.ts",
        "src/**/*.d.ts",
        // Barrel files — re-exports only, no runtime logic to cover.
        "src/transports/index.ts",
        "src/integrations/index.ts",
        // Type-only module (pure interfaces). Covered by consumer tests.
        "src/hooks.ts",
      ],
      thresholds: {
        lines: 80,
        statements: 80,
        branches: 75,
        functions: 80,
      },
    },
  },
});
